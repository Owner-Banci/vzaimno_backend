from __future__ import annotations

from typing import Any

from app.db import pool_stats


_PROM_ENABLED = True

try:
    from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
except Exception:
    _PROM_ENABLED = False

    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _NoopMetric:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def labels(self, *args: Any, **kwargs: Any) -> "_NoopMetric":
            return self

        def inc(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def set(self, *_args: Any, **_kwargs: Any) -> None:
            return None

        def observe(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    Counter = Gauge = Histogram = _NoopMetric  # type: ignore

    def generate_latest() -> bytes:  # type: ignore
        return b""


http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)

db_pool_connections_active = Gauge(
    "db_pool_connections_active",
    "Approx active DB pool connections",
    ["pool"],
)

db_pool_connections_idle = Gauge(
    "db_pool_connections_idle",
    "Approx idle DB pool connections",
    ["pool"],
)

db_pool_connections_waiting = Gauge(
    "db_pool_connections_waiting",
    "Approx waiting DB pool requests",
    ["pool"],
)

auth_login_attempts_total = Counter(
    "auth_login_attempts_total",
    "Authentication login attempts",
    ["result"],
)

auth_lockouts_total = Counter(
    "auth_lockouts_total",
    "Authentication lockouts",
)

uploads_total = Counter(
    "uploads_total",
    "Upload processing results",
    ["result"],
)

external_call_duration_seconds = Histogram(
    "external_call_duration_seconds",
    "Duration of external service calls",
    ["service"],
)

external_call_errors_total = Counter(
    "external_call_errors_total",
    "Errors from external service calls",
    ["service"],
)


def observe_http(method: str, path: str, status_code: int, duration_seconds: float) -> None:
    http_requests_total.labels(method=method, path=path, status=str(status_code)).inc()
    http_request_duration_seconds.labels(method=method, path=path).observe(max(0.0, duration_seconds))


def observe_login_attempt(result: str) -> None:
    auth_login_attempts_total.labels(result=result).inc()


def observe_lockout() -> None:
    auth_lockouts_total.inc()


def observe_upload(result: str) -> None:
    uploads_total.labels(result=result).inc()


def observe_external_call(service: str, duration_seconds: float, *, error: bool = False) -> None:
    external_call_duration_seconds.labels(service=service).observe(max(0.0, duration_seconds))
    if error:
        external_call_errors_total.labels(service=service).inc()


def refresh_db_pool_metrics() -> None:
    stats = pool_stats()
    for pool_name in ("write", "read"):
        item = stats.get(pool_name) or {}
        # psycopg_pool stats keys vary by version; try several known keys.
        active = int(item.get("pool_in_use") or item.get("in_use") or item.get("used") or 0)
        waiting = int(item.get("requests_waiting") or item.get("waiting") or 0)
        max_size = int(item.get("pool_max") or item.get("max_size") or 0)
        idle = max(0, max_size - active)
        db_pool_connections_active.labels(pool=pool_name).set(active)
        db_pool_connections_idle.labels(pool=pool_name).set(idle)
        db_pool_connections_waiting.labels(pool=pool_name).set(waiting)


def metrics_payload() -> bytes:
    refresh_db_pool_metrics()
    return generate_latest()


def metrics_enabled() -> bool:
    return _PROM_ENABLED
