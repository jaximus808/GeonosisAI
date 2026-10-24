"""Structured JSON logging for the orchestrator (one JSON object per line on stderr).

    log = get_logger("bus")
    log.info("handler.ok", extra={"fields": {"handler": "x", "ms": 12}})

``ticket_id`` comes from a contextvar that the bus sets around every handler call, so any log
line written inside a handler carries the ticket it is working on.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

ticket_id_var: contextvars.ContextVar[object | None] = contextvars.ContextVar("ticket_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "fields", "color_message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        tid = getattr(record, "ticket_id", None) or ticket_id_var.get()
        if tid is not None:
            out["ticket_id"] = tid
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            out.update(fields)
        for k, v in record.__dict__.items():  # plain extra={...} keys too
            if k not in _RESERVED and k not in out and not k.startswith("_"):
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Route the root logger (and uvicorn's) through the JSON formatter. Idempotent."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"rdf.orchestrator.{name}")
