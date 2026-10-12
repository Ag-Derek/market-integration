"""
SQLite storage for the Bank of Ghana's FX fixes: one row per business
day per pair, exactly as BoG published it (its pair code and direction).
Converting to cedis per unit happens on the way out (app.fx.normalize).

Like watchlists, this is kept across restarts and never rebuilt: the
history is pulled from BoG once, and each refresh adds what's new.
Writing the same (date, pair) again updates its rates but keeps when it
was first fetched, so re-fetching an overlap is harmless.
"""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.fx import Fix

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS fx_rates (
        date TEXT NOT NULL,          -- YYYY-MM-DD
        pair TEXT NOT NULL,          -- as BoG publishes it: USDGHS, GHSNGN, ...
        currency TEXT NOT NULL,      -- BoG's name for it
        buying REAL NOT NULL,
        selling REAL NOT NULL,
        mid REAL NOT NULL,
        fetched_at TEXT NOT NULL,    -- when we first fetched it: BoG publishes no time
                                     -- for its fix, so this is how we learn it
        PRIMARY KEY (date, pair)
    )
"""


class FxStore:
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

    def upsert(self, fixes: Iterable[Fix]) -> None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO fx_rates (date, pair, currency, buying, selling, mid, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(date, pair) DO UPDATE SET currency = excluded.currency, "
                "buying = excluded.buying, selling = excluded.selling, mid = excluded.mid",
                [(f.date.isoformat(), f.pair, f.currency, f.buying, f.selling, f.mid, fetched_at) for f in fixes],
            )
            conn.commit()
        finally:
            conn.close()

    def all(self) -> list[Fix]:
        """Every stored fix, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT date, currency, pair, buying, selling, mid FROM fx_rates ORDER BY date, pair"
            ).fetchall()
        finally:
            conn.close()
        return [Fix(date.fromisoformat(d), c, p, b, s, m) for d, c, p, b, s, m in rows]

    def first_fetched(self, day: date) -> Optional[datetime]:
        """When we first fetched any of `day`'s fixes, or None."""
        conn = self._connect()
        try:
            (at,) = conn.execute("SELECT MIN(fetched_at) FROM fx_rates WHERE date = ?", (day.isoformat(),)).fetchone()
        finally:
            conn.close()
        return datetime.fromisoformat(at) if at else None

    def span(self) -> Optional[tuple[date, date]]:
        """(earliest, latest) date stored, or None while empty."""
        conn = self._connect()
        try:
            lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM fx_rates").fetchone()
        finally:
            conn.close()
        return (date.fromisoformat(lo), date.fromisoformat(hi)) if lo else None
