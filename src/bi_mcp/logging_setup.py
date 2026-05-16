"""Logging configuration for the Blue Iris MCP server.

Silent by default. When ``BI_MCP_DEBUG=1`` is set, configures:
  * a stderr handler (visible in Claude Code's MCP debug view)
  * a rotating file handler at a platform-appropriate cache dir

Sensitive fields (``password``, ``response``, ``session``) are redacted from
log records to avoid leaking credentials into the file.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from platformdirs import user_cache_dir

_REDACT_PATTERN = re.compile(
    r'("(?:password|response|session)"\s*:\s*")[^"]*(")',
    flags=re.IGNORECASE,
)

LOGGER_NAME = "bi_mcp"


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _REDACT_PATTERN.sub(r"\1***\2", record.msg)
        if record.args:
            redacted_args = []
            for arg in record.args:
                if isinstance(arg, str):
                    redacted_args.append(_REDACT_PATTERN.sub(r"\1***\2", arg))
                else:
                    redacted_args.append(arg)
            record.args = tuple(redacted_args)
        return True


def setup_logging() -> logging.Logger:
    """Configure the package logger. Idempotent."""
    logger = logging.getLogger(LOGGER_NAME)

    if getattr(logger, "_bi_mcp_configured", False):
        return logger

    debug_enabled = os.environ.get("BI_MCP_DEBUG", "0").strip() in ("1", "true", "yes", "on")

    if not debug_enabled:
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.CRITICAL + 1)
        logger._bi_mcp_configured = True  # type: ignore[attr-defined]
        return logger

    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = _RedactingFilter()

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(fmt)
    stderr_handler.addFilter(redactor)
    logger.addHandler(stderr_handler)

    cache_dir = Path(user_cache_dir("bi-mcp"))
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            cache_dir / "server.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning("Could not open log file in %s: %s", cache_dir, e)

    logger._bi_mcp_configured = True  # type: ignore[attr-defined]
    logger.debug("Logging initialised (debug mode)")
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
