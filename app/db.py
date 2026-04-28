# app/db.py
# Productionized minimal DB helper (psycopg3)

import logging
import os
import threading
from dotenv import load_dotenv
import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import tuple_row

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

logger = logging.getLogger("vzaimno")
DB_CONNECT_TIMEOUT_SECONDS = max(1, int(os.getenv("DB_CONNECT_TIMEOUT_SECONDS", "5")))


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
conn: psycopg.Connection | None


def _open_connection() -> tuple[psycopg.Connection, str]:
    db_conn, effective_url = _connect_with_local_fallback(DATABASE_URL)
    db_conn.autocommit = True
    return db_conn, effective_url


def _reset_connection() -> psycopg.Connection:
    global conn, DATABASE_URL
    with _conn_lock:
        if conn is not None and not bool(getattr(conn, "closed", False)):
            try:
                conn.close()
            except Exception:
                pass
        conn, DATABASE_URL = _open_connection()
        return conn


def _get_connection() -> psycopg.Connection:
    global conn
    with _conn_lock:
        if conn is None or bool(getattr(conn, "closed", False)):
            conn = _reset_connection()
        return conn


def _is_recoverable_disconnect(exc: Exception) -> bool:
    message = str(exc).lower()
    return "connection is closed" in message or "server closed the connection" in message


# global connection with lazy self-heal
conn, DATABASE_URL = _open_connection()


def _run_query(method: str, query: str, params: tuple = ()):
    last_exc: Exception | None = None
    for attempt in range(2):
        active_conn = _get_connection()
        try:
            with active_conn.cursor() as cur:
                cur.execute(query, params)
                if method == "one":
                    return cur.fetchone()
                if method == "all":
                    return cur.fetchall()
                return True
        except psycopg.OperationalError as exc:
            last_exc = exc
            if attempt == 0 and (_is_recoverable_disconnect(exc) or bool(getattr(active_conn, "closed", False))):
                _reset_connection()
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


def pool_stats() -> dict[str, dict[str, int]]:
    """
    Compatibility shim for metrics module.

    This project currently uses a single psycopg connection (no psycopg_pool),
    but app.metrics expects pool-like stats for "write" and "read" pools.
    """
    closed = int(bool(getattr(conn, "closed", False)))
    max_size = 1
    in_use = 0 if closed else 1
    return {
        "write": {
            "pool_in_use": in_use,
            "pool_max": max_size,
            "requests_waiting": 0,
        },
        "read": {
            "pool_in_use": in_use,
            "pool_max": max_size,
            "requests_waiting": 0,
        },
    }
