"""
SQLite storage for watchlists: one row per user, the symbols as a JSON
array in pin order. Each list has a table of its own: `watchlists` for
the ticker's equities, `commodity_watchlists` for GCX contracts.

Unlike the instrument and company tables, this is user data, not a copy
of a seed file: it is created if missing and never rebuilt, so
watchlists survive restarts.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

TABLES = ("watchlists", "commodity_watchlists")

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS {table} (
        user_id TEXT PRIMARY KEY,
        symbols TEXT NOT NULL,      -- JSON array, in pin order
        updated_at TEXT NOT NULL
    )
"""


class WatchlistStore:
    def __init__(self, db_path: "Path | str", table: str = "watchlists"):
        if table not in TABLES:
            raise ValueError(f"unknown watchlist table {table!r}")
        self._db_path = Path(db_path)
        self._table = table
        conn = self._connect()
        try:
            conn.execute(_CREATE_TABLE.format(table=table))
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def get(self, user_id: str) -> Optional[dict]:
        """{"symbols": [...], "updated_at": iso} for a user who has saved
        a watchlist, else None."""
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT symbols, updated_at FROM {self._table} WHERE user_id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        return {"symbols": json.loads(row[0]), "updated_at": row[1]} if row else None

    def put(self, user_id: str, symbols: list[str]) -> dict:
        """Replace the user's watchlist with `symbols` (already validated)."""
        updated_at = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {self._table} (user_id, symbols, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET symbols = excluded.symbols, updated_at = excluded.updated_at",
                (user_id, json.dumps(symbols), updated_at),
            )
            conn.commit()
        finally:
            conn.close()
        return {"symbols": list(symbols), "updated_at": updated_at}
