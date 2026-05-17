"""`@log_tool_usage` — free observability for every tool.

Wraps a tool dispatch function. On every call, logs:

  * tool name
  * duration in ms
  * success / error kind
  * response size (best-effort: ``len(str(result))``)

Quiet by default — only emits when ``BI_MCP_DEBUG=1`` has activated the
package logger (see ``logging_setup.py``).
"""

from __future__ import annotations

import time
from functools import wraps
from typing import Any, Callable

from ..errors import BiError
from ..logging_setup import get_logger

log = get_logger()

ToolFn = Callable[..., Any]


def log_tool_usage(name: str) -> Callable[[ToolFn], ToolFn]:
    """Decorator factory. Usage: ``@log_tool_usage("bi_get_status")``."""

    def decorator(fn: ToolFn) -> ToolFn:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except BiError as exc:
                dur_ms = (time.perf_counter() - t0) * 1000
                log.info(
                    "tool=%s status=error kind=%s dur_ms=%.1f msg=%s",
                    name,
                    exc.kind,
                    dur_ms,
                    exc,
                )
                raise
            except Exception as exc:
                dur_ms = (time.perf_counter() - t0) * 1000
                log.warning(
                    "tool=%s status=unexpected dur_ms=%.1f type=%s msg=%s",
                    name,
                    dur_ms,
                    type(exc).__name__,
                    exc,
                )
                raise
            dur_ms = (time.perf_counter() - t0) * 1000
            try:
                size = len(str(result))
            except Exception:
                size = -1
            log.info(
                "tool=%s status=ok dur_ms=%.1f bytes=%d",
                name,
                dur_ms,
                size,
            )
            return result

        return wrapped

    return decorator
