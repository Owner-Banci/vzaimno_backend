from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, TypeVar

from app.config import get_float, get_int
from app.logging_utils import logger
from app.metrics import observe_external_call


T = TypeVar("T")


CB_WINDOW_SECONDS = max(10.0, get_float("EXTERNAL_CB_WINDOW_SECONDS", 60.0))
CB_FAILURE_THRESHOLD = max(1, get_int("EXTERNAL_CB_FAILURE_THRESHOLD", 5))
CB_OPEN_SECONDS = max(1.0, get_float("EXTERNAL_CB_OPEN_SECONDS", 30.0))
RETRY_BACKOFF_BASE_S = max(0.01, get_float("EXTERNAL_RETRY_BACKOFF_BASE_S", 0.2))


@dataclass
class _CircuitState:
    failures: list[float] = field(default_factory=list)
    opened_until: float = 0.0


_circuit_lock = Lock()
_circuits: dict[str, _CircuitState] = {}


def _state(service: str) -> _CircuitState:
    state = _circuits.get(service)
    if state is None:
        state = _CircuitState()
        _circuits[service] = state
    return state


def _prune_failures(state: _CircuitState, now_mono: float) -> None:
    threshold = now_mono - CB_WINDOW_SECONDS
    state.failures = [ts for ts in state.failures if ts >= threshold]


def _is_circuit_open(service: str) -> tuple[bool, float]:
    now_mono = time.monotonic()
    with _circuit_lock:
        state = _state(service)
        if state.opened_until > now_mono:
            return True, max(0.0, state.opened_until - now_mono)
        return False, 0.0


def _mark_success(service: str) -> None:
    with _circuit_lock:
        state = _state(service)
        state.failures.clear()
        state.opened_until = 0.0


def _mark_failure(service: str) -> bool:
    now_mono = time.monotonic()
    with _circuit_lock:
        state = _state(service)
        _prune_failures(state, now_mono)
        state.failures.append(now_mono)
        if len(state.failures) >= CB_FAILURE_THRESHOLD:
            state.failures.clear()
            state.opened_until = now_mono + CB_OPEN_SECONDS
            return True
        return False


def call_external_sync(
    service: str,
    fn: Callable[[], T],
    *,
    retries: int = 1,
    backoff_base_s: float | None = None,
    fallback: T | None = None,
) -> T | None:
    """
    Execute sync external IO with retry, metrics and a simple in-process circuit breaker.
    """
    is_open, retry_after_s = _is_circuit_open(service)
    if is_open:
        logger.warning(
            "external_call_circuit_open",
            extra={"service": service, "retry_after_s": int(round(retry_after_s)), "status_code": 0},
        )
        return fallback

    total_attempts = max(1, int(retries) + 1)
    backoff = backoff_base_s if backoff_base_s is not None else RETRY_BACKOFF_BASE_S
    last_error: str = "unknown"

    for attempt in range(1, total_attempts + 1):
        started_at = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            duration = max(0.0, time.perf_counter() - started_at)
            observe_external_call(service, duration, error=True)
            last_error = str(exc)
            tripped = _mark_failure(service)
            if tripped:
                logger.warning(
                    "external_call_circuit_tripped",
                    extra={"service": service, "attempt": attempt, "error": last_error, "status_code": 0},
                )
            if attempt < total_attempts:
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            logger.warning(
                "external_call_failed",
                extra={"service": service, "attempt": attempt, "error": last_error, "status_code": 0},
            )
            return fallback
        else:
            duration = max(0.0, time.perf_counter() - started_at)
            observe_external_call(service, duration, error=False)
            _mark_success(service)
            return result

    logger.warning(
        "external_call_failed",
        extra={"service": service, "attempt": total_attempts, "error": last_error, "status_code": 0},
    )
    return fallback
