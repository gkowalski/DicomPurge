"""Application-wide logging: rotating file at ~/dicompurge.log plus a Qt signal bridge."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal

LOG_PATH = Path(os.path.expanduser("~")) / "dicompurge.log"
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class LogBridge(QObject):
    """Turns log records into Qt signals so they can cross into the GUI thread."""

    record = Signal(int, str, str)  # levelno, level name, formatted line


class QtLogHandler(logging.Handler):
    def __init__(self, bridge: LogBridge) -> None:
        super().__init__()
        self.bridge = bridge

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # pragma: no cover - never let logging kill the app
            return
        # Queued automatically when emitted from a worker thread.
        self.bridge.record.emit(record.levelno, record.levelname, msg)


_bridge: LogBridge | None = None
_file_handler: logging.Handler | None = None


def configure_logging(level: int = logging.DEBUG) -> LogBridge:
    """Install file + Qt handlers on the root logger. Safe to call once."""
    global _bridge, _file_handler
    if _bridge is not None:
        return _bridge

    _bridge = LogBridge()
    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    root = logging.getLogger()
    root.setLevel(level)

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    _file_handler = file_handler

    qt_handler = QtLogHandler(_bridge)
    qt_handler.setFormatter(formatter)
    qt_handler.setLevel(logging.DEBUG)
    root.addHandler(qt_handler)

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    stream.setLevel(logging.INFO)
    root.addHandler(stream)

    logging.getLogger(__name__).info("Logging initialised -> %s", LOG_PATH)
    return _bridge


def truncate_log_file() -> None:
    """Empty ~/dicompurge.log without losing the open handler."""
    handler = _file_handler
    if handler is None:
        LOG_PATH.write_text("", encoding="utf-8")
        return
    try:
        handler.acquire()
        if getattr(handler, "stream", None):
            handler.stream.close()
        LOG_PATH.write_text("", encoding="utf-8")
        handler.stream = handler._open()  # type: ignore[attr-defined]
    finally:
        handler.release()
