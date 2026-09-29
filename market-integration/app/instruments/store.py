"""
SQLite copy of the instrument master, backing the /instruments API.

data/instruments.json is the source of truth: seed() rebuilds the whole
table from it on every startup, so an edited, added or removed entry
shows up after a restart and the table never drifts from the file. The
table is derived data, so seed() also drops and recreates it -- a
column added to Instrument needs no migration.
"""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.models.instrument import Instrument

_COLUMNS = (
    "symbol", "name", "asset_class", "kind", "sector", "currency", "isin", "status",
    "issuer", "segment", "tenor", "maturity_date", "coupon_rate",
)

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS instruments (
        symbol TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        asset_class TEXT NOT NULL,
        kind TEXT,
        sector TEXT NOT NULL,
        currency TEXT NOT NULL,
        isin TEXT,
        status TEXT NOT NULL,
        issuer TEXT,
        segment TEXT,
        tenor TEXT,
        maturity_date TEXT,
        coupon_rate REAL,
        updated_at TEXT NOT NULL
    )
"""


def _to_db(value):
    return value.isoformat() if isinstance(value, date) else value


class InstrumentStore:
    def __init__(self, db_path: "Path | str"):
        self._db_path = Path(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.execute(_CREATE_TABLE)
            conn.commit()
        finally:
            conn.close()

    def seed(self, instruments: Iterable[Instrument]) -> int:
        """Rebuild the table from `instruments`, atomically. Returns the
        number of rows written."""
        updated_at = datetime.now(timezone.utc).isoformat()
        rows = [
            tuple(_to_db(getattr(i, c)) for c in _COLUMNS) + (updated_at,)
            for i in instruments
        ]
        conn = self._connect()
        # Explicit transaction: sqlite3's implicit one doesn't cover DDL,
        # so DROP/CREATE would otherwise commit on their own and a reader
        # could see a missing or empty table mid-rebuild.
        conn.isolation_level = None
        try:
            conn.execute("BEGIN")
            try:
                conn.execute("DROP TABLE IF EXISTS instruments")
                conn.execute(_CREATE_TABLE)
                conn.executemany(
                    f"INSERT INTO instruments ({', '.join(_COLUMNS)}, updated_at) "
                    f"VALUES ({', '.join('?' * (len(_COLUMNS) + 1))})",
                    rows,
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        return len(rows)

    def list(
        self, asset_class: Optional[str] = None, sector: Optional[str] = None
    ) -> list[Instrument]:
        query = f"SELECT {', '.join(_COLUMNS)} FROM instruments WHERE 1 = 1"
        params: list = []
        if asset_class is not None:
            query += " AND asset_class = ?"
            params.append(asset_class)
        if sector is not None:
            query += " AND sector = ? COLLATE NOCASE"
            params.append(sector)
        query += " ORDER BY symbol"
        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [Instrument(**dict(zip(_COLUMNS, r))) for r in rows]

    def get(self, symbol: str) -> Optional[Instrument]:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT {', '.join(_COLUMNS)} FROM instruments WHERE symbol = ?", (symbol,)
            ).fetchone()
        finally:
            conn.close()
        return Instrument(**dict(zip(_COLUMNS, row))) if row else None
