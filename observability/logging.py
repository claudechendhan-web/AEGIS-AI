from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import TextIO

_RESERVED_RECORD_FIELDS = {
    "args",
    "asctime",
    "created",
    "exc_info",
    "exc_text",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "message",
    "module",
    "msecs",
    "msg",
    "name",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "stack_info",
    "taskName",
    "thread",
    "threadName",
}


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_FIELDS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


def configure_logging(
    level: str | int = "INFO", stream: TextIO | None = None
) -> logging.Handler:
    if isinstance(level, str):
        normalized_level = logging.getLevelName(level.upper())
        if not isinstance(normalized_level, int):
            raise TypeError(f"unknown log level: {level}")
    else:
        normalized_level = level
    root_logger = logging.getLogger()
    root_logger.setLevel(normalized_level)
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(StructuredFormatter())
    root_logger.addHandler(handler)
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event_type: str, **fields: object) -> None:
    logger.info(event_type, extra={"event": event_type, **fields})
