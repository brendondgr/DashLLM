"""Structured logging for every subsystem.

Two sinks:
- console: human-readable, for dev
- rotating file (``logs/relay.log``): JSON lines, one object per event,
  machine-greppable. Every subsystem (proxy, router, health, tunnels,
  telemetry, stats, admin, frontend) logs through here with a
  ``subsystem``-tagged logger, so the file is the single audit stream.

Use ``get_logger("proxy")`` and pass structured context via
``log.info("forwarded", extra={"data": {...}})``.
"""

import json
import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

_CONFIGURED = False


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": round(record.created, 3),
            "time": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(record.created)
            ),
            "level": record.levelname,
            "subsystem": record.name.removeprefix("relay."),
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data is not None:
            out["data"] = data
        if record.exc_info and record.exc_info[0] is not None:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{time.strftime('%H:%M:%S', time.localtime(record.created))} "
            f"{record.levelname:<7} [{record.name.removeprefix('relay.')}] "
            f"{record.getMessage()}"
        )
        data = getattr(record, "data", None)
        if data:
            base += f" {json.dumps(data, default=str)}"
        if record.exc_info and record.exc_info[0] is not None:
            base += "\n" + self.formatException(record.exc_info)
        return base


def setup_logging(log_dir: Path, level: str = "INFO") -> None:
    """Idempotent root setup for the ``relay`` logger tree + uvicorn."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("relay")
    root.setLevel(level.upper())

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "relay.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(JsonLineFormatter())
    root.addHandler(file_handler)

    # Route uvicorn's own logs into the same JSON file sink.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.addHandler(file_handler)


def get_logger(subsystem: str) -> logging.Logger:
    return logging.getLogger(f"relay.{subsystem}")
