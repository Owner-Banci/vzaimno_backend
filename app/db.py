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
