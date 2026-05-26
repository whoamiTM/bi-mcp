"""Blue Iris .reg camera-settings parser.

Camera settings exports from BI are *binary regf hives*, not text registry
files. Parsing them uses the ``python-registry`` package, which ships as a
normal ``bi-mcp`` dependency. The parser runs in-process — no sibling venv
or subprocess fork.

The returned dict shape is::

    {
        "<subkey path>": {
            "<value name>": <value>,
            ...
        },
        ...
    }

Empty subkeys / value-less subkeys are dropped. Binary values
(``REG_BINARY``) are encoded as ``{"_type": "binary", "hex": ..., "len": ...}``.
"""

from __future__ import annotations

import os
import re
import time
import warnings
from binascii import hexlify
from pathlib import Path
from typing import Any

from Registry import Registry, RegistryParse

from .errors import BiBadRequest, BiError, BiNotFound

# BI_MCP_REG_DIR resolves at *call* time relative to the current working
# directory, not at import time relative to the source file. This is because
# bi-mcp can be installed as a wheel / via uvx, in which case __file__ lives
# inside the venv's site-packages tree — not next to the user's
# cam settings/ directory. Claude Code launches the server from the project
# dir (e.g. .../blueiris/), so CWD is the right anchor.
#
# Default (when unset) is ./cam settings/ relative to the launch directory.

STALE_THRESHOLD_DAYS = 7.0

# Camera short names per BI convention: alphanumerics, underscore, hyphen.
# Reject anything else — most importantly path separators and "..", which
# would let a caller escape BI_MCP_REG_DIR.
_CAMERA_SHORT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

_DEPRECATED_VENV_ENV = "BI_MCP_REG_VENV_PYTHON"
_deprecation_warned = False


def _warn_deprecated_venv_env() -> None:
    """Emit a one-shot DeprecationWarning if the legacy env var is set."""
    global _deprecation_warned
    if _deprecation_warned:
        return
    if os.environ.get(_DEPRECATED_VENV_ENV):
        warnings.warn(
            f"{_DEPRECATED_VENV_ENV} is no longer used — `python-registry` is "
            "now a normal bi-mcp dependency and the parser runs in-process. "
            "Unset this variable; it will be removed in v0.2.",
            DeprecationWarning,
            stacklevel=3,
        )
    _deprecation_warned = True


def _serialize(v: Any) -> Any:
    # python-registry returns mostly native types for ints / strings; binary
    # values come back as bytes. Hex-encode bytes so the result is portable.
    if isinstance(v, bytes):
        return {"_type": "binary", "hex": hexlify(v).decode("ascii"), "len": len(v)}
    if isinstance(v, (list, tuple)):
        return [_serialize(x) for x in v]
    return v


def _walk(key: Any, prefix: str = "") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    name = prefix or key.name()
    vals: dict[str, Any] = {}
    for val in key.values():
        try:
            vals[val.name()] = _serialize(val.value())
        except (UnicodeDecodeError, RegistryParse.RegistryStructureDoesNotExist):
            continue
    if vals:
        out[name] = vals
    for sub in key.subkeys():
        sub_path = f"{name}\\{sub.name()}" if name else sub.name()
        out.update(_walk(sub, sub_path))
    return out


def _parse_hive(path: Path, key_path: str | None) -> dict[str, dict[str, Any]]:
    """Parse a regf hive file. Returns the flat-by-subkey-path dict.

    Raises ``Registry.RegistryKeyNotFoundException`` if ``key_path`` is set
    and doesn't exist in the hive. Other parser failures propagate as
    whatever exception ``python-registry`` raises.
    """
    r = Registry.Registry(str(path))
    root = r.root()
    if key_path:
        key = root.find_key(key_path)
        return _walk(key, prefix=key_path)
    out: dict[str, dict[str, Any]] = {}
    for sub in root.subkeys():
        out.update(_walk(sub, prefix=sub.name()))
    return out


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


def list_reg_cameras() -> list[str]:
    """Return camera short names with a ``.reg`` export in ``_reg_dir()``,
    sorted alphabetically. Used by audit tools that need to walk the whole
    install without a pre-built roster."""
    reg_dir = _reg_dir()
    if not reg_dir.is_dir():
        return []
    shorts: list[str] = []
    for p in reg_dir.glob("*.reg"):
        stem = p.stem
        if _CAMERA_SHORT_RE.fullmatch(stem):
            shorts.append(stem)
    shorts.sort()
    return shorts


def parse_reg(camera_short: str, key_path: str | None = None) -> tuple[dict[str, Any], float]:
    """Parse the .reg file for ``camera_short``.

    Returns ``(parsed_data, mtime_age_days)``. ``parsed_data`` is keyed by
    backslash-joined registry subpath relative to the hive root (which is
    itself the camera short name); see module docstring for the shape.

    Raises ``BiNotFound`` if the .reg file is missing or ``key_path`` is
    absent from the hive. ``BiError`` for any other parse failure — the
    in-process parser surfaces python-registry exceptions directly, so the
    catch-all here is the only crash-isolation boundary the MCP server has.
    """
    _warn_deprecated_venv_env()
    reg_file = resolve_reg_file(camera_short)
    age = mtime_age_days(reg_file)

    try:
        parsed = _parse_hive(reg_file, key_path)
    except Registry.RegistryKeyNotFoundException as e:
        raise BiNotFound(f"key {key_path!r} not found in {reg_file.name}") from e
    except Exception as e:
        raise BiError(f"failed to parse {reg_file}: {e}") from e

    return parsed, age
