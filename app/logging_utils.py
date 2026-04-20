from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any


_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
_user_id_var: ContextVar[str | None] = ContextVar("user_id", default=None)
_admin_id_var: ContextVar[str | None] = ContextVar("admin_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": _request_id_var.get(),
            "user_id": _user_id_var.get(),
            "admin_id": _admin_id_var.get(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("_"):
                continue
            if key in {
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
                "module",
                "msecs",
                "message",
                "msg",
                "name",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "stack_info",
                "thread",
                "threadName",
            }:
                continue
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_json_logging() -> None:
    logger_obj = logging.getLogger("vzaimno")
    if logger_obj.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger_obj.setLevel(logging.INFO)
    logger_obj.addHandler(handler)
    logger_obj.propagate = False


def ensure_request_id(raw_request_id: str | None = None) -> str:
    value = (raw_request_id or "").strip()
    if value:
        return value
    return str(uuid.uuid4())


def set_request_context(request_id: str | None) -> None:
    _request_id_var.set(request_id)


def bind_user(user_id: str | None = None, admin_id: str | None = None) -> None:
    _user_id_var.set(user_id)
    _admin_id_var.set(admin_id)


def clear_request_context() -> None:
    _request_id_var.set(None)
    _user_id_var.set(None)
    _admin_id_var.set(None)


def log_http_request(*, method: str, path: str, status_code: int, started_at: float, remote_ip_hash: str | None = None) -> None:
    duration_ms = int(max(0.0, (time.perf_counter() - started_at) * 1000))
    logger.info(
        "http_request",
        extra={
            "method": method,
            "path": path,
            "status_code": int(status_code),
            "duration_ms": duration_ms,
            "remote_ip_hash": remote_ip_hash,
        },
    )


configure_json_logging()
logger = logging.getLogger("vzaimno")

