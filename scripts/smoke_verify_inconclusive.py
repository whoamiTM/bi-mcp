"""Regression tests for the verify-inconclusive contract.

Three scenarios, each fires `bi_set_camera audio=...` against SecCam_10 with
a fault injected at a specific layer, and asserts the dispatcher returns
a structured response that:
  * Keeps ``ok=true`` (BI accepted the write — duplicate-action hazard if
    we surfaced this as ok=false; Codex adversarial review round 2,
    2026-05-23).
  * Sets ``verified=false`` to express that the post-read couldn't confirm.
  * Carries the specific ``verify_error_kind`` (auth-class vs network-class)
    so callers can escalate auth differently from network.

Scenarios:
  1. dispatcher-layer: `BiClients.verify_call` itself raises the base
     `BiVerifyInconclusive` — proves the dispatcher catches+surfaces it.
     Kind defaults to ``verify_inconclusive`` (no subclass).
  2. policy-layer (auth blip): `BiClient.call` inside the fresh client
     raises `BiAuthFailed` → `verify_call` maps to `BiVerifyAuthBlip`,
     kind=``verify_auth_blip`` (Codex round 1 / round 3).
  3. policy-layer (network blip): `BiClient.call` inside the fresh client
     raises `BiUnreachable` → `verify_call` maps to `BiVerifyUnreachable`,
     kind=``verify_unreachable`` (Codex round 2 / round 3).

Run from bi-mcp/ as:
    .venv/bin/python scripts/smoke_verify_inconclusive.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from bi_mcp.client import BiClient, BiClients, from_env  # noqa: E402
from bi_mcp.errors import (  # noqa: E402
    BiAuthFailed,
    BiUnreachable,
    BiVerifyInconclusive,
)
from bi_mcp.logging_setup import setup_logging  # noqa: E402
from bi_mcp.tools import TOOLS  # noqa: E402

setup_logging()

CAMERA = "SecCam_10"


def _assert_inconclusive(result, scenario, expected_kind):
    """Assert the response carries the no-retry-trigger inconclusive shape.

    Contract (post Codex round 3, 2026-05-23):
      * ok=True  — BI accepted the write; do NOT signal retry-triggering
                   failure here, several ops aren't idempotent.
      * verified=False — verification couldn't confirm.
      * verify_error_kind=<expected_kind> — auth vs network vs base.
      * verify_error present — human-readable detail.
    """
    problems = []
    if result.get("ok") is not True:
        problems.append(f"expected ok=True (write accepted), got ok={result.get('ok')!r}")
    if result.get("verified") is not False:
        problems.append(f"expected verified=False, got verified={result.get('verified')!r}")
    if result.get("verify_error_kind") != expected_kind:
        problems.append(
            f"expected verify_error_kind={expected_kind!r}, got "
            f"{result.get('verify_error_kind')!r}"
        )
    if "verify_error" not in result:
        problems.append("expected 'verify_error' message in result")
    if result.get("op") != "audio" or result.get("camera") != CAMERA:
        problems.append(f"unexpected op/camera in result: {result!r}")
    if problems:
        print(f"[{scenario}] FAIL:")
        for p in problems:
            print(f"  - {p}")
        return False
    print(f"[{scenario}] OK: ok=true, verified=false, verify_error_kind={expected_kind!r}.")
    return True


def _run_with_patched_verify_call(client, target_audio, patch_func):
    """Replace BiClients.verify_call with patch_func, run the tool, restore."""
    original_func = BiClients.__dict__["verify_call"].__func__
    BiClients.verify_call = staticmethod(patch_func)
    try:
        return TOOLS["bi_set_camera"](
            client, {"camera": CAMERA, "audio": target_audio}
        )
    finally:
        BiClients.verify_call = staticmethod(original_func)


def _run_with_fresh_client_blip(client, target_audio, raise_exc_factory):
    """Inject a fault into ONLY the throwaway verify client.

    `verify_call` is left untouched — we want to exercise its real
    `try/except BiAuthFailed | BiUnreachable -> BiVerifyInconclusive`
    mapping. We do that by wrapping `fresh_admin_session` so the yielded
    `BiClient` has its `call` method pre-replaced with one that raises.
    The pre-read / write paths use `admin_call` on the singleton, which
    is unaffected.
    """
    # The real attribute is a @contextmanager-wrapped function (already a
    # plain attribute on the class, not a descriptor — no __func__).
    original_session = BiClients.__dict__["fresh_admin_session"]

    def patched_session(self):
        # Reuse the real factory but mutate the yielded client's `call`.
        # We re-implement the contextmanager body directly so we can rewrite
        # `fresh.call` before yielding.
        src = self.admin_or_raise()
        fresh = BiClient(host=src.host, port=src.port, user=src.user, password=src._password)

        def faulty_call(cmd, **_payload):
            raise raise_exc_factory(cmd)

        fresh.call = faulty_call  # type: ignore[method-assign]
        try:
            yield fresh
        finally:
            fresh.close()

    import contextlib as _ctx

    BiClients.fresh_admin_session = _ctx.contextmanager(patched_session)
    try:
        return TOOLS["bi_set_camera"](
            client, {"camera": CAMERA, "audio": target_audio}
        )
    finally:
        BiClients.fresh_admin_session = original_session


def main():
    client = from_env()

    pre = client.admin_call("camconfig", camera=CAMERA)
    pre_audio = pre.get("audio")
    target_audio = not pre_audio
    print(f"pre: audio={pre_audio}, will flip to {target_audio}\n")

    all_ok = True
    try:
        # --- Scenario 1: dispatcher catches BiVerifyInconclusive ---
        print("scenario 1: verify_call raises BiVerifyInconclusive directly")

        def patched_v1(*_a, **_k):
            raise BiVerifyInconclusive("simulated: direct from verify_call")

        result = _run_with_patched_verify_call(client, target_audio, patched_v1)
        print(f"  result: {result}")
        # Scenario 1 raises the base class directly, so kind stays at the base value.
        all_ok &= _assert_inconclusive(result, "dispatcher", "verify_inconclusive")
        # Restore pre-state between scenarios.
        TOOLS["bi_set_camera"](client, {"camera": CAMERA, "audio": pre_audio})
        print()

        # --- Scenario 2: BiAuthFailed under verify_call's policy boundary ---
        print("scenario 2: BiClient.call raises BiAuthFailed during verify")
        result = _run_with_fresh_client_blip(
            client,
            target_audio,
            lambda cmd: BiAuthFailed(f"simulated auth blip on cmd={cmd}"),
        )
        print(f"  result: {result}")
        all_ok &= _assert_inconclusive(result, "auth policy", "verify_auth_blip")
        TOOLS["bi_set_camera"](client, {"camera": CAMERA, "audio": pre_audio})
        print()

        # --- Scenario 3: BiUnreachable under verify_call's policy boundary ---
        print("scenario 3: BiClient.call raises BiUnreachable during verify")
        result = _run_with_fresh_client_blip(
            client,
            target_audio,
            lambda cmd: BiUnreachable(f"simulated network blip on cmd={cmd}"),
        )
        print(f"  result: {result}")
        all_ok &= _assert_inconclusive(result, "unreachable policy", "verify_unreachable")
        TOOLS["bi_set_camera"](client, {"camera": CAMERA, "audio": pre_audio})
        print()

    finally:
        # Belt-and-braces restore.
        post = client.admin_call("camconfig", camera=CAMERA)
        if post.get("audio") != pre_audio:
            print("final restore: forcing audio back to pre-state")
            TOOLS["bi_set_camera"](client, {"camera": CAMERA, "audio": pre_audio})
        post = client.admin_call("camconfig", camera=CAMERA)
        print(f"final BI state: audio={post.get('audio')}")

    if all_ok:
        print("\nALL OK: verify-inconclusive contract holds for auth + network blips.")
        return 0
    print("\nFAIL: one or more scenarios did not surface verify-inconclusive.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
