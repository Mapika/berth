from __future__ import annotations

import sqlite3
from typing import Any


def row_get(row: sqlite3.Row, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default
