# app/db.py
# Productionized minimal DB helper (psycopg3)

import logging
import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from dotenv import load_dotenv
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

logger = logging.getLogger("vzaimno")
DB_CONNECT_TIMEOUT_SECONDS = max(1, int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "5")))
DB_POOL_MIN_SIZE = max(0, int(os.getenv("DB_POOL_MIN_SIZE", "1")))
DB_POOL_MAX_SIZE = max(1, int(os.getenv("DB_POOL_MAX_SIZE", "10")))
DB_POOL_TIMEOUT_SECONDS = max(1, int(os.getenv("DB_POOL_TIMEOUT_SECONDS", "10")))
DB_POOL_MAX_IDLE_SECONDS = max(1, int(os.getenv("DB_POOL_MAX_IDLE_SECONDS", "300")))


def _connect_with_local_fallback(database_url: str) -> tuple[psycopg.Connection, str]:
    primary_conninfo = make_conninfo(database_url, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)
    try:
        return psycopg.connect(primary_conninfo, row_factory=tuple_row), database_url
    except Exception as primary_exc:
        info = conninfo_to_dict(database_url)
        host = str(info.get("host") or "")
        port = str(info.get("port") or "")
        if host in {"localhost", "127.0.0.1", "::1"} and port == "5433":
            fallback_url = make_conninfo(database_url, port=5432, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)
            try:
                conn = psycopg.connect(fallback_url, row_factory=tuple_row)
                logger.warning(
                    "db_port_fallback_5432",
                    extra={"status_code": 0, "event": "db_port_fallback_5432"},
                )
                return conn, fallback_url
            except Exception:
                pass
        raise primary_exc


_conn_lock = threading.RLock()
_transaction_conn: ContextVar[psycopg.Connection | None] = ContextVar("transaction_conn", default=None)
pool: ConnectionPool | None = None
conn: psycopg.Connection | None = None


def _resolve_database_url(database_url: str) -> str:
    db_conn, effective_url = _connect_with_local_fallback(database_url)
    try:
        db_conn.close()
    except Exception:
        pass
    return make_conninfo(effective_url, connect_timeout=DB_CONNECT_TIMEOUT_SECONDS)


def _open_pool() -> tuple[ConnectionPool, str]:
    effective_url = _resolve_database_url(DATABASE_URL)
    db_pool = ConnectionPool(
        conninfo=effective_url,
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
        timeout=DB_POOL_TIMEOUT_SECONDS,
        max_idle=DB_POOL_MAX_IDLE_SECONDS,
        kwargs={"row_factory": tuple_row, "autocommit": True},
        open=True,
    )
    return db_pool, effective_url


def _reset_pool() -> ConnectionPool:
    global pool, DATABASE_URL
    with _conn_lock:
        if pool is not None:
            try:
                pool.close()
            except Exception:
                pass
        pool, DATABASE_URL = _open_pool()
        return pool


def _get_pool() -> ConnectionPool:
    global pool
    with _conn_lock:
        if pool is None or bool(getattr(pool, "closed", False)):
            pool = _reset_pool()
        return pool


def _is_recoverable_disconnect(exc: Exception) -> bool:
    message = str(exc).lower()
    return "connection is closed" in message or "server closed the connection" in message


# global pool with lazy self-heal
pool, DATABASE_URL = _open_pool()


def _run_query(method: str, query: str, params: tuple = ()):
    active_tx_conn = _transaction_conn.get()
    if active_tx_conn is not None:
        with active_tx_conn.cursor() as cur:
            cur.execute(query, params)
            if method == "one":
                return cur.fetchone()
            if method == "all":
                return cur.fetchall()
            return True

    last_exc: Exception | None = None
    for attempt in range(2):
        db_pool = _get_pool()
        try:
            with db_pool.connection() as active_conn:
                with active_conn.cursor() as cur:
                    cur.execute(query, params)
                    if method == "one":
                        return cur.fetchone()
                    if method == "all":
                        return cur.fetchall()
                    return True
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt == 0 and _is_recoverable_disconnect(exc):
                _reset_pool()
                continue
            raise
    if last_exc is not None:
        raise last_exc
    return None


def fetch_one(query: str, params: tuple = ()):
    return _run_query("one", query, params)


def fetch_all(query: str, params: tuple = ()):
    return _run_query("all", query, params)


def execute(query: str, params: tuple = ()):
    return _run_query("execute", query, params)


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """
    Run existing fetch/execute helpers on one pooled connection inside a DB
    transaction. Nested calls reuse the same connection and open a savepoint.
    """
    active_conn = _transaction_conn.get()
    if active_conn is not None:
        with active_conn.transaction():
            yield active_conn
        return

    db_pool = _get_pool()
    with db_pool.connection() as active_conn:
        token = _transaction_conn.set(active_conn)
        try:
            with active_conn.transaction():
                yield active_conn
        finally:
            _transaction_conn.reset(token)


def close_pool() -> None:
    global pool
    with _conn_lock:
        if pool is not None:
            pool.close()
            pool = None


def pool_stats() -> dict[str, dict[str, int]]:
    db_pool = _get_pool()
    try:
        raw_stats = db_pool.get_stats()
    except Exception:
        raw_stats = {}
    pool_size = int(raw_stats.get("pool_size", 0) or 0)
    available = int(raw_stats.get("pool_available", 0) or 0)
    max_size = int(getattr(db_pool, "max_size", DB_POOL_MAX_SIZE) or DB_POOL_MAX_SIZE)
    in_use = max(0, pool_size - available)
    waiting = int(raw_stats.get("requests_waiting", 0) or 0)
    return {
        "write": {
            "pool_in_use": in_use,
            "pool_max": max_size,
            "requests_waiting": waiting,
        },
        "read": {
            "pool_in_use": in_use,
            "pool_max": max_size,
            "requests_waiting": waiting,
        },
    }
