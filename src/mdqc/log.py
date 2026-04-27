"""structlog setup. JSON to file, human-readable to console.

Every long-running process (service, tray) calls configure_logging() once at
startup. Per-module loggers are obtained via `log = structlog.get_logger(__name__)`.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Literal

import structlog

from mdqc.config import paths

LogLevel = Literal["error", "warn", "info", "debug", "trace"]

_LEVEL_MAP = {
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "trace": logging.DEBUG,  # Python has no trace; treat as debug
}


def configure_logging(
    level: LogLevel = "info",
    *,
    log_to_file: bool = True,
    log_to_console: bool = True,
    log_file: Path | None = None,
) -> None:
    """Set up stdlib + structlog. Idempotent within a process."""
    py_level = _LEVEL_MAP[level]
    handlers: list[logging.Handler] = []

    if log_to_console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setLevel(py_level)
        ch.setFormatter(_ConsoleFormatter())
        handlers.append(ch)

    if log_to_file:
        target = log_file or (paths.log_dir() / "mdqc.log")
        target.parent.mkdir(parents=True, exist_ok=True)
        # 10 MB x 10 files = 100 MB cap (matches Rust's tracing-appender setup).
        fh = logging.handlers.RotatingFileHandler(
            target, maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        fh.setLevel(py_level)
        fh.setFormatter(_JsonFormatter())
        handlers.append(fh)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(py_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from pythonjsonlogger import jsonlogger

        if not hasattr(self, "_inner"):
            self._inner = jsonlogger.JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        return self._inner.format(record)


class _ConsoleFormatter(logging.Formatter):
    """Compact human-readable format for stderr."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%dT%H:%M:%S")
        return f"{ts} {record.levelname:<5} [{record.name}] {record.getMessage()}"


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
