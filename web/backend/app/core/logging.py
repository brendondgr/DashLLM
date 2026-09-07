"""Structured logging for every subsystem.

Two sinks:
- console: human-readable, for dev
- rotating file (``logs/relay.log``): JSON lines, one object per event,
  machine-greppable. Every subsystem (proxy, router, health, tunnels,
  telemetry, stats, admin, frontend) logs through here with a
  ``subsystem``-tagged logger, so the file is the single audit stream.

Use ``get_logger("proxy")`` and pass structured context via
``log.info("forwarded", extra={"data": {...}})``.

**Both sinks sit behind a queue.** ``logging`` handlers are synchronous, and
the hot path emits several records per request — so on a busy relay the event
loop was doing a formatted ``write()`` per line, and every 10 MB it did the
rename dance of a log rotation mid-request. A ``QueueHandler`` makes the
emitting side an append to a deque; a listener thread owns the file and the
console. Nothing about the call sites changes.
"""

import atexit
import json
import logging
import queue
import sys
import time
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

_CONFIGURED = False
_LISTENER: QueueListener | None = None

# Bounded so a logging storm can't become a memory incident. Overflow drops
# the record rather than blocking the producer: under the load this exists to
# survive, a dropped log line is cheaper than a stalled request.
_QUEUE_DEPTH = 20_000


class _DroppingQueue(queue.SimpleQueue):
    """``SimpleQueue``, but it refuses records past ``maxsize``.

    ``QueueHandler.emit`` calls ``put_nowait`` and reports any exception
    through ``handleError``, so the drop is quietly absorbed here instead.
    """

    def __init__(self, maxsize: int):
        super().__init__()
        self.maxsize = maxsize
        self.dropped = 0

    def put_nowait(self, item) -> None:
        if self.qsize() >= self.maxsize:
            self.dropped += 1
            return
        super().put_nowait(item)


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
    global _CONFIGURED, _LISTENER
    if _CONFIGURED:
        return
    _CONFIGURED = True

    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("relay")
    root.setLevel(level.upper())

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(ConsoleFormatter())

    file_handler = RotatingFileHandler(
        log_dir / "relay.log", maxBytes=10 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(JsonLineFormatter())

    # The only handler any logger gets is the queue; the listener thread owns
    # the real sinks, so no caller ever touches the disk.
    log_queue = _DroppingQueue(_QUEUE_DEPTH)
    _LISTENER = QueueListener(
        log_queue, console, file_handler, respect_handler_level=True)
    _LISTENER.start()
    atexit.register(_LISTENER.stop)

    queue_handler = QueueHandler(log_queue)
    root.addHandler(queue_handler)

    # Route uvicorn's own logs into the same JSON file sink.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.addHandler(queue_handler)


def get_logger(subsystem: str) -> logging.Logger:
    return logging.getLogger(f"relay.{subsystem}")


def flush_logs(timeout: float = 2.0) -> None:
    """Block until the listener has drained, so a caller (a test, a shutdown
    path) can read the log file back and see what it just wrote."""
    if _LISTENER is None:
        return
    deadline = time.monotonic() + timeout
    while not _LISTENER.queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
