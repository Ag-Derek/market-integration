"""
SQLite storage for commodity prices: one row per source, series and
month, exactly as the source published it (its series name and unit).
Every series a source publishes is kept, offered or not, so offering
another commodity needs no new pull.

Like the FX fixes, this is kept across restarts and never rebuilt. A
source re-publishes its whole history each month, so writing the same
(source, series, month) again replaces its price, which is how a
revision lands, but keeps when it was first fetched.
"""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.commodities import Price, month_key, parse_month

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS commodity_prices (
        source TEXT NOT NULL,        -- app.commodities.SOURCES key: worldbank, ...
        series TEXT NOT NULL,        -- the source's name for it: "Crude oil, Brent"
        month TEXT NOT NULL,         -- YYYY-MM, the month it averages
        unit TEXT NOT NULL,          -- as published: "$/bbl"
        value REAL NOT NULL,         -- US dollars per unit
        fetched_at TEXT NOT NULL,    -- when we first fetched it
        PRIMARY KEY (source, series, month)
    )
"""


class CommodityStore:
    def __init__(self, db_path: "Path | str"):
        self._db_path = Path(db_path)
        conn = self._connect()
        try:
            conn.execute(_CREATE_TABLE)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def upsert(self, prices: Iterable[Price]) -> None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO commodity_prices (source, series, month, unit, value, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(source, series, month) DO UPDATE SET unit = excluded.unit, value = excluded.value",
                [(p.source, p.series, month_key(p.month), p.unit, p.value, fetched_at) for p in prices],
            )
            conn.commit()
        finally:
            conn.close()

    def series(self, source: str, names: Iterable[str]) -> list[Price]:
        """Every stored price of the named series from `source`, oldest first."""
        names = list(names)
        if not names:
            return []
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT series, unit, month, value FROM commodity_prices "
                f"WHERE source = ? AND series IN ({','.join('?' * len(names))}) ORDER BY series, month",
                [source, *names],
            ).fetchall()
        finally:
            conn.close()
        return [Price(source, s, u, parse_month(m), v) for s, u, m, v in rows]

    def first_fetched(self, source: str, series: str, month: date) -> Optional[datetime]:
        """When we first fetched that price, or None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT fetched_at FROM commodity_prices WHERE source = ? AND series = ? AND month = ?",
                (source, series, month_key(month)),
            ).fetchone()
        finally:
            conn.close()
        return datetime.fromisoformat(row[0]) if row else None
