from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Sequence

from fastapi import HTTPException

from app.config import app_env, get_env


logger = logging.getLogger("vzaimno")
_warned_dev_missing_redis = False
_warned_dev_unavailable_redis = False


@dataclass(frozen=True)
class LimitRule:
    amount: int
    window_seconds: int


class RateLimitError(HTTPException):
    def __init__(self, retry_after: int):
        super().__init__(status_code=429, detail="Too Many Requests", headers={"Retry-After": str(retry_after)})


@lru_cache(maxsize=1)
def redis_url() -> str:
    return (get_env("REDIS_URL", "") or "").strip()


def _new_redis_client() -> Any:
    try:
        from redis.asyncio import Redis  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "redis package is required for rate limiting. Install with `pip install redis`."
        ) from exc

    url = redis_url()
    if not url:
        raise RuntimeError("REDIS_URL is not set")
    return Redis.from_url(url, decode_responses=True)


_redis_client_ref: Any | None = None
_redis_client_loop: asyncio.AbstractEventLoop | None = None


def redis_client() -> Any:
    global _redis_client_ref
    global _redis_client_loop
    current_loop = asyncio.get_running_loop()
    if _redis_client_ref is None or _redis_client_loop is not current_loop:
        _redis_client_ref = _new_redis_client()
        _redis_client_loop = current_loop
    return _redis_client_ref


def _window_bucket(now_ts: int, window_seconds: int) -> tuple[int, int]:
    window_start = now_ts - (now_ts % window_seconds)
    retry_after = max(1, window_seconds - (now_ts - window_start))
    return window_start, retry_after


async def enforce_rate_limit(scope: str, identity: str, limits: Sequence[LimitRule]) -> None:
    global _warned_dev_missing_redis
    global _warned_dev_unavailable_redis

    if not limits:
        return

    if not redis_url():
        if app_env() == "dev":
            if not _warned_dev_missing_redis:
                logger.warning(
                    "rate_limiter_disabled_in_dev",
                    extra={"status_code": 0, "event": "rate_limiter_disabled_in_dev"},
                )
                _warned_dev_missing_redis = True
            return
        raise HTTPException(status_code=503, detail="Rate limiter is not configured")

    try:
        client = redis_client()
    except Exception as exc:
        if app_env() == "dev":
            if not _warned_dev_unavailable_redis:
                logger.warning(
                    "rate_limiter_unavailable_in_dev",
                    extra={"status_code": 0, "event": "rate_limiter_unavailable_in_dev"},
                )
                _warned_dev_unavailable_redis = True
            return
        raise HTTPException(status_code=503, detail="Rate limiter is unavailable") from exc

    now_ts = int(time.time())

    worst_retry_after = 0
    for rule in limits:
        window_start, retry_after = _window_bucket(now_ts, rule.window_seconds)
        key = f"rl:{scope}:{identity}:{rule.window_seconds}:{window_start}"
        value = await client.incr(key)
        if value == 1:
            await client.expire(key, rule.window_seconds)
        if value > rule.amount:
            worst_retry_after = max(worst_retry_after, retry_after)

    if worst_retry_after > 0:
        raise RateLimitError(retry_after=worst_retry_after)


async def check_redis_ready() -> bool:
    if not redis_url():
        return False
    try:
        return bool(await redis_client().ping())
    except Exception:
        return False
