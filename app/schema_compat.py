from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from app.db import fetch_all


@lru_cache(maxsize=128)
def get_table_columns(table_name: str) -> frozenset[str]:
    rows = fetch_all(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table_name,),
    )
    return frozenset(str(row[0]) for row in rows)


def clear_schema_cache() -> None:
    get_table_columns.cache_clear()


def table_has_column(table_name: str, column_name: str) -> bool:
    return column_name in get_table_columns(table_name)


def table_has_columns(table_name: str, column_names: Iterable[str]) -> bool:
    columns = get_table_columns(table_name)
    return all(column_name in columns for column_name in column_names)
