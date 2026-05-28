"""Shared time-parsing helpers for BI tools."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any


_REL_RE = re.compile(r"^-(\d+)([smhd])$")
_REL_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_since(value: Any, *, arg_name: str = "since") -> int:
    """Parse a time arg into a UTC epoch (seconds).

    Accepts:
      * int / numeric string — passed through as epoch seconds.
      * Relative shorthand like ``-15m``, ``-2h``, ``-1d``.
      * ISO-8601 string parseable by ``datetime.fromisoformat``.
    """
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"{arg_name} must be an int (UTC seconds), ISO-8601 string, "
            "or relative shorthand like '-15m', '-2h', '-1d'"
        )
    s = value.strip()
    if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
        return int(s)
    m = _REL_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return int(time.time()) - n * _REL_UNITS[unit]
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(
            f"{arg_name}={value!r} not parseable. Accepted: int epoch seconds, "
            "ISO-8601 ('2026-05-23T14:00:00Z'), or relative ('-15m','-2h','-1d')."
        ) from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())
