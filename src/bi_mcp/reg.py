"""Blue Iris .reg camera-settings parser wrapper.

Camera settings exports from BI are *binary regf hives*, not text registry
files. Parsing them requires the ``python-registry`` package, which lives in
a sibling virtualenv at ``../.reg-venv/`` (relative to the bi-mcp repo's
parent — i.e. the ``blueiris/`` project root).

To avoid forcing ``python-registry`` into bi-mcp's own deps, we subprocess
to that venv's Python interpreter and run a small inline script that dumps
the hive as JSON, then parse the result back here.

The script-output JSON shape is::

    {
        "<subkey path>": {
            "<value name>": <value>,
            ...
        },
        ...
    }

Empty subkeys / value-less subkeys are dropped. Binary values
(``REG_BINARY``) are hex-encoded.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .errors import BiBadRequest, BiError, BiNotFound

# Default locations resolve at *call* time relative to the current working
# directory, not at import time relative to the source file. This is because
# bi-mcp can be installed as a wheel / via uvx, in which case __file__ lives
# inside the venv's site-packages tree — not next to the user's .reg-venv/
# and cam settings/ directories. Claude Code launches the server from the
# project dir (e.g. .../blueiris/), so CWD is the right anchor.
#
# Either env var overrides its default:
#   BI_MCP_REG_VENV_PYTHON — absolute path to a Python with python-registry
#   BI_MCP_REG_DIR         — absolute path to the directory of <short>.reg files
#
# The defaults (when neither override is set) are ./.reg-venv/bin/python3
# and ./cam settings/ relative to the launch directory.

STALE_THRESHOLD_DAYS = 7.0

# Camera short names per BI convention: alphanumerics, underscore, hyphen.
# Reject anything else — most importantly path separators and "..", which
# would let a caller escape BI_MCP_REG_DIR.
_CAMERA_SHORT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


_PARSE_SCRIPT = r"""
import json, sys
from binascii import hexlify
from Registry import Registry, RegistryParse


def serialize(v):
    # python-registry returns mostly native types for ints / strings; binary
    # values come back as bytes. Hex-encode bytes so the JSON is portable.
    if isinstance(v, bytes):
        return {"_type": "binary", "hex": hexlify(v).decode("ascii"), "len": len(v)}
    if isinstance(v, (list, tuple)):
        return [serialize(x) for x in v]
    return v


def walk(key, prefix=""):
    out = {}
    name = prefix or key.name()
    vals = {}
    for val in key.values():
        try:
            vals[val.name()] = serialize(val.value())
        except (UnicodeDecodeError, RegistryParse.RegistryStructureDoesNotExist):
            continue
    if vals:
        out[name] = vals
    for sub in key.subkeys():
        sub_path = f"{name}\\{sub.name()}" if name else sub.name()
        out.update(walk(sub, sub_path))
    return out


def main():
    path = sys.argv[1]
    requested = sys.argv[2] if len(sys.argv) > 2 else ""
    r = Registry.Registry(path)
    root = r.root()
    if requested:
        try:
            key = root.find_key(requested)
        except Registry.RegistryKeyNotFoundException:
            print(json.dumps({"_error": f"key '{requested}' not found"}))
            return 1
        out = walk(key, prefix=requested)
    else:
        out = {}
        for sub in root.subkeys():
            out.update(walk(sub, prefix=sub.name()))
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _reg_venv_python() -> Path:
    """Resolve the .reg-venv Python interpreter.

    Env override wins; otherwise probe both standard venv layouts under
    ``./.reg-venv/`` relative to CWD: POSIX (``bin/python3``) and Windows
    (``Scripts/python.exe``). Blue Iris runs primarily on Windows, so the
    Windows layout has to work out of the box.

    Returns the first candidate that exists, or the POSIX path as the
    "best-guess" return so the caller's not-found error message points at
    a reasonable default. ``parse_reg()`` checks existence before invoking.
    """
    override = os.environ.get("BI_MCP_REG_VENV_PYTHON")
    if override:
        return Path(override).expanduser()
    base = Path.cwd() / ".reg-venv"
    candidates = [
        base / "bin" / "python3",       # POSIX
        base / "bin" / "python",        # POSIX (some venvs only ship `python`)
        base / "Scripts" / "python.exe",  # Windows
        base / "Scripts" / "python3.exe",  # Windows (rare)
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _reg_dir() -> Path:
    """Resolve the directory where .reg exports live. Env override wins;
    default is ``./cam settings/`` relative to the current working directory."""
    override = os.environ.get("BI_MCP_REG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.cwd() / "cam settings"


def resolve_reg_file(camera_short: str) -> Path:
    """Map a camera short name to its .reg file path.

    Validates the short name against a strict pattern (alphanumeric +
    ``_``/``-``) before composing the path — without this, a caller could
    pass ``../../etc/passwd`` and escape BI_MCP_REG_DIR. Raises
    ``BiNotFound`` if no matching file exists, or ``BiBadRequest`` if the
    name is malformed.
    """
    if not camera_short:
        raise BiBadRequest("bi_get_reg requires a camera short name")
    if not _CAMERA_SHORT_RE.fullmatch(camera_short):
        raise BiBadRequest(
            f"Invalid camera short name {camera_short!r}: must contain only "
            "letters, digits, underscores, and hyphens (no path separators, "
            "spaces, or '..')."
        )
    reg_dir = _reg_dir()
    target = reg_dir / f"{camera_short}.reg"
    if not target.exists():
        raise BiNotFound(
            f"No .reg export found at {target}. Re-export the camera (right-click "
            f"in Blue Iris → Camera settings → Copy/import → Export) and save it "
            f"into {reg_dir}, or set BI_MCP_REG_DIR to point at a different "
            f"directory."
        )
    return target


def mtime_age_days(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 86400.0


def parse_reg(camera_short: str, key_path: str | None = None) -> tuple[dict[str, Any], float]:
    """Parse the .reg file for ``camera_short``.

    Returns ``(parsed_data, mtime_age_days)``. ``parsed_data`` is keyed by
    backslash-joined registry subpath relative to the hive root (which is
    itself the camera short name); see module docstring for the JSON shape.

    Raises ``BiNotFound`` if the .reg file is missing, ``BiError`` if the
    parser subprocess fails for any reason.
    """
    reg_file = resolve_reg_file(camera_short)
    age = mtime_age_days(reg_file)

    py = _reg_venv_python()
    if not py.exists():
        raise BiError(
            f"Cannot find the .reg-venv Python at {py}. Either (a) create the venv "
            "in the launch directory: `python -m venv .reg-venv` then "
            "`.reg-venv/bin/pip install python-registry` (POSIX) or "
            "`.reg-venv\\Scripts\\pip install python-registry` (Windows); or "
            "(b) set BI_MCP_REG_VENV_PYTHON to the absolute path of an existing "
            "Python interpreter with python-registry installed."
        )

    cmd: list[str] = [str(py), "-c", _PARSE_SCRIPT, str(reg_file)]
    if key_path:
        cmd.append(key_path)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
    except FileNotFoundError as e:
        raise BiError(f"Failed to invoke .reg-venv Python: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise BiError(f".reg parser timed out after 30s on {reg_file}") from e

    # Try to parse stdout first — the helper script signals "key not found" via
    # an `{"_error": ...}` JSON payload on stdout with rc=1. We need to surface
    # that as BiNotFound *before* raising the generic rc-mismatch BiError, so
    # callers get an actionable error for typoed/absent subkeys.
    parsed: Any = None
    stdout = proc.stdout.strip()
    if stdout:
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            parsed = None

    if isinstance(parsed, dict) and "_error" in parsed:
        raise BiNotFound(parsed["_error"])

    if proc.returncode != 0:
        stderr = proc.stderr.strip() or "(no stderr)"
        raise BiError(f".reg parser failed (rc={proc.returncode}): {stderr}")

    if parsed is None:
        raise BiError(f".reg parser output was not JSON: {proc.stdout[:200]}")

    return parsed, age
