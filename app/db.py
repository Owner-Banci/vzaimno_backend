# app/db.py
# Productionized minimal DB helper (psycopg3)

import os
from dotenv import load_dotenv
import psycopg
from psycopg.rows import tuple_row

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

# global connection (MVP)
conn = psycopg.connect(DATABASE_URL, row_factory=tuple_row)
conn.autocommit = True


def fetch_one(query: str, params: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def fetch_all(query: str, params: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def execute(query: str, params: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(query, params)
        return True


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
