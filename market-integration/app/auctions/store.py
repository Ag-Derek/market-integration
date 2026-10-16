"""
SQLite storage for the Bank of Ghana's auction results: one row per
security per issue date, exactly as published. Kept across restarts and
never rebuilt, like the FX fixes (app/fx/store.py): writing the same
(issue date, security) again updates its rates but keeps when it was
first fetched, which is when we learned the result.
"""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.auctions import AuctionResult

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS auction_results (
        issue_date TEXT NOT NULL,     -- YYYY-MM-DD
        security TEXT NOT NULL,       -- as BoG writes it: 91 DAY BILL, 2 YR FXR NOTE, ...
        tender INTEGER NOT NULL,
        discount_rate REAL NOT NULL,
        interest_rate REAL NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (issue_date, security)
    )
"""


class AuctionStore:
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

    def upsert(self, results: Iterable[AuctionResult]) -> None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO auction_results (issue_date, security, tender, discount_rate, interest_rate, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(issue_date, security) DO UPDATE SET tender = excluded.tender, "
                "discount_rate = excluded.discount_rate, interest_rate = excluded.interest_rate",
                [(r.issue_date.isoformat(), r.security, r.tender, r.discount_rate, r.interest_rate, fetched_at)
                 for r in results],
            )
            conn.commit()
        finally:
            conn.close()

    def all(self) -> list[AuctionResult]:
        """Every stored result, oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT issue_date, tender, security, discount_rate, interest_rate FROM auction_results "
                "ORDER BY issue_date, security"
            ).fetchall()
        finally:
            conn.close()
        return [AuctionResult(date.fromisoformat(d), t, s, dr, ir) for d, t, s, dr, ir in rows]

    def latest_date(self) -> Optional[date]:
        conn = self._connect()
        try:
            (d,) = conn.execute("SELECT MAX(issue_date) FROM auction_results").fetchone()
        finally:
            conn.close()
        return date.fromisoformat(d) if d else None
