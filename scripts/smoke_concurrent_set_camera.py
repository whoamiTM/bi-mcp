"""Concurrent smoke test for bi_set_camera verify isolation.

Two threads share ONE BiClients singleton and fire bi_set_camera against
SecCam_10 with overlapping verify windows. Pre-fix this would corrupt the
shared admin session (admin.session = None mid-call). Post-fix each call's
verify helper uses its own fresh BiClient and the singleton is untouched.

Run from bi-mcp/ as:
    .venv/bin/python scripts/smoke_concurrent_set_camera.py

Restores SecCam_10 audio to True on exit regardless of outcome.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from bi_mcp.client import from_env  # noqa: E402
from bi_mcp.logging_setup import setup_logging  # noqa: E402
from bi_mcp.tools import TOOLS  # noqa: E402

setup_logging()

CAMERA = "SecCam_10"


def fire(client, args, label, results):
    t0 = time.monotonic()
    try:
        r = TOOLS["bi_set_camera"](client, args)
        results[label] = ("ok", time.monotonic() - t0, r)
    except Exception as e:
        results[label] = ("err", time.monotonic() - t0, f"{type(e).__name__}: {e}")


def main():
    client = from_env()
    # Force initial login so the singleton is fully warm before we race.
    client.admin_login()

    # Toggle pair: A flips audio false, B flips output to its current value
    # (idempotent op, still exercises camconfig write+verify).
    pre = client.admin_call("camconfig", camera=CAMERA)
    pre_audio = pre.get("audio")
    pre_output = pre.get("output")
    print(f"pre: audio={pre_audio} output={pre_output}")

    # Both threads target the SAME op + value. Even if BI serializes the two
    # writes, both should observe the new value on verify. The point is to
    # exercise overlapping verify-helper sessions on the shared BiClients,
    # not to test BI's own concurrent-write semantics.
    target_audio = not pre_audio
    results: dict[str, tuple] = {}
    t_a = threading.Thread(
        target=fire,
        args=(client, {"camera": CAMERA, "audio": target_audio}, "A", results),
    )
    t_b = threading.Thread(
        target=fire,
        args=(client, {"camera": CAMERA, "audio": target_audio}, "B", results),
    )

    print("firing two bi_set_camera calls on shared BiClients...")
    t0 = time.monotonic()
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()
    print(f"both returned after {time.monotonic() - t0:.2f}s\n")

    for label in ("A", "B"):
        status, dur, payload = results[label]
        print(f"--- {label} ({status}, {dur:.2f}s) ---")
        print(payload)
        print()

    # Restore.
    print("restoring SecCam_10 to pre-state...")
    TOOLS["bi_set_camera"](client, {"camera": CAMERA, "audio": pre_audio})

    post = client.admin_call("camconfig", camera=CAMERA)
    print(f"post-restore: audio={post.get('audio')} output={post.get('output')}")

    failed = [k for k, v in results.items() if v[0] != "ok"]
    if failed:
        print(f"\nFAIL: {failed}")
        return 1
    print("\nOK: both concurrent calls verified cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
