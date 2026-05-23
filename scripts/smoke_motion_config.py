"""Smoke test for bi_get_camera_motion_config.

Three assertions against SecCam_3:
  1. Shaped call returns non-empty `motion` (12 keys) and `post` (2 keys), plus
     verbatim `motion_raw` / `post_raw` twins.
  2. raw=true returns the unshaped camconfig payload containing `setmotion` —
     proves we routed through admin camconfig, not the camlist fallback.
  3. motion.sense matches bi_get_reg(key_path="Motion").sense when the .reg is
     fresh (<7 days). Skipped with a warning otherwise — the parity check is
     only meaningful while .reg reflects current state.

Note on the .reg path: BI's profile-1 motion config lives under the bare
`Motion` subkey, NOT `Motion\\3`. The `Motion\\N` keys are off-by-one
(jaydeel ipcamtalk thread 85627). All cameras in this install are profile=1.

Run from bi-mcp/ as:
    .venv/bin/python scripts/smoke_motion_config.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from bi_mcp.client import BiClients, from_env  # noqa: E402
from bi_mcp.errors import BiError, BiNotFound  # noqa: E402
from bi_mcp.logging_setup import setup_logging  # noqa: E402
from bi_mcp.tools import TOOLS  # noqa: E402

setup_logging()

CAMERA = "SecCam_3"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    tag = "OK" if ok else "FAIL"
    line = f"[{tag}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)
    return ok


def main() -> int:
    client = from_env()
    all_ok = True

    # --- Assertion 1: shaped call has motion + post + raw twins ---
    shaped = TOOLS["bi_get_camera_motion_config"](client, {"short": CAMERA})
    motion = shaped.get("motion") or {}
    post = shaped.get("post") or {}
    motion_raw = shaped.get("motion_raw") or {}
    post_raw = shaped.get("post_raw") or {}
    all_ok &= _check(
        "motion non-empty",
        bool(motion) and "sense" in motion and "contrast" in motion,
        f"keys={sorted(motion)[:6]}…",
    )
    all_ok &= _check(
        "post non-empty",
        bool(post) and "timed_interval" in post,
        f"keys={sorted(post)}",
    )
    all_ok &= _check(
        "motion_raw == motion (verbatim twin)",
        motion == motion_raw,
        "pass-through shaper must not mutate values",
    )
    all_ok &= _check(
        "post_raw == post (verbatim twin)",
        post == post_raw,
        "pass-through shaper must not mutate values",
    )
    all_ok &= _check(
        "_source = camconfig",
        shaped.get("_source") == "camconfig",
    )

    # --- Assertion 2: raw=true returns unshapen camconfig payload ---
    raw = TOOLS["bi_get_camera_motion_config"](client, {"short": CAMERA, "raw": True})
    all_ok &= _check(
        "raw=true contains setmotion",
        isinstance(raw, dict) and "setmotion" in raw,
        "confirms admin camconfig path (camlist would lack this key)",
    )
    all_ok &= _check(
        "raw=true contains setpost",
        isinstance(raw, dict) and "setpost" in raw,
    )
    if isinstance(raw, dict) and "setmotion" in raw:
        all_ok &= _check(
            "raw setmotion matches shaped motion",
            raw["setmotion"] == motion,
            "pass-through must agree with raw",
        )

    # --- Assertion 3: motion.sense matches .reg Motion.sense when fresh ---
    reg = TOOLS["bi_get_reg"](client, {"camera": CAMERA, "key_path": "Motion"})
    meta = reg.get("meta") or {}
    if meta.get("stale"):
        print(
            f"[SKIP] .reg parity check — {CAMERA}.reg is "
            f"{meta.get('mtime_age_days')}d old (>7d); re-export to enable this check"
        )
    else:
        reg_motion = (reg.get("data") or {}).get("Motion") or {}
        reg_sense = reg_motion.get("sense")
        live_sense = motion.get("sense")
        all_ok &= _check(
            f"motion.sense ({live_sense}) == .reg Motion.sense ({reg_sense})",
            reg_sense == live_sense,
            "live wire value should match the freshly-exported .reg",
        )

    # --- Assertion 4: unknown camera raises BiNotFound ---
    # Empirically (2026-05-23) BI 5.9.9.71 returns {} for an unknown short.
    try:
        TOOLS["bi_get_camera_motion_config"](client, {"short": "ZZZ_does_not_exist"})
        all_ok &= _check("unknown camera raises BiNotFound", False, "no exception raised")
    except BiNotFound:
        all_ok &= _check("unknown camera raises BiNotFound", True)
    except Exception as e:
        all_ok &= _check(
            "unknown camera raises BiNotFound",
            False,
            f"wrong exception type: {type(e).__name__}: {e}",
        )

    # --- Assertion 5: malformed response raises BiError (not silent empty) ---
    # Simulate a future BI build that drops/renames setmotion by monkey-patching
    # admin_call to return a dict missing the required subtrees. The tool calls
    # `client.admin_call(...)` where `client` is a BiClients instance, so we
    # patch on that class.
    original_admin_call = BiClients.admin_call

    def fake_admin_call(self, cmd, **payload):
        # Return a plausible-looking but invariant-violating payload.
        return {"enabled": True, "profile": 1, "record": 2}  # no setmotion, no setpost

    BiClients.admin_call = fake_admin_call  # type: ignore[method-assign]
    try:
        try:
            result = TOOLS["bi_get_camera_motion_config"](client, {"short": CAMERA})
            all_ok &= _check(
                "malformed response raises BiError",
                False,
                f"got silent result instead: {result!r}",
            )
        except BiNotFound as e:
            # BiNotFound is also a BiError subclass; but for a non-empty dict
            # missing the invariants we want the more informative BiError path.
            all_ok &= _check(
                "malformed response raises BiError (not BiNotFound)",
                False,
                f"got BiNotFound for non-empty malformed payload: {e}",
            )
        except BiError as e:
            ok = "setmotion" in str(e) and "setpost" in str(e)
            all_ok &= _check(
                "malformed response raises BiError",
                ok,
                f"message must mention the missing keys; got: {str(e)[:120]}",
            )
        # raw=true must bypass the invariant check and return the wire payload as-is.
        raw_through = TOOLS["bi_get_camera_motion_config"](
            client, {"short": CAMERA, "raw": True}
        )
        all_ok &= _check(
            "raw=true bypasses the invariant check",
            isinstance(raw_through, dict) and "setmotion" not in raw_through,
            "raw=true must surface the malformed payload verbatim, not raise",
        )
    finally:
        BiClients.admin_call = original_admin_call  # type: ignore[method-assign]

    if all_ok:
        print("\nALL OK: bi_get_camera_motion_config contract holds.")
        return 0
    print("\nFAIL: one or more smoke assertions did not pass.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
