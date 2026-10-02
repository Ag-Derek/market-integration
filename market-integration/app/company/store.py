"""
SQLite copy of the company information, behind
GET /instruments/{symbol}/description.

Three tables, all keyed by the instrument master's symbol:

  company_profiles    one row per company; each maintained field F is
                      three columns, F, F_source and F_as_of
  company_officers    one row per officer, with its source and as_of
  company_financials  one row per company and fiscal year; each figure
                      with its own _source and _as_of columns

Like the instruments table, data/company_profiles.json is the source of
truth: seed() rebuilds all three from it on every startup, in one
transaction, so the tables never drift from the file and a column added
to a model needs no migration.
"""

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.models.company import (
    FINANCIAL_FIELDS,
    PROFILE_FIELDS,
    Company,
    CompanyOfficer,
    CompanyProfile,
    FinancialYear,
)


def _sourced_columns(fields: Iterable[str]) -> list[str]:
    return [c for f in fields for c in (f, f"{f}_source", f"{f}_as_of")]


_PROFILE_COLUMNS = _sourced_columns(PROFILE_FIELDS)
_FINANCIAL_COLUMNS = _sourced_columns(FINANCIAL_FIELDS)
_OFFICER_COLUMNS = ["name", "role", "display_order", "source", "as_of"]
_COLUMN_TYPES = {"employees": "INTEGER", "shares_outstanding": "INTEGER", "revenue": "REAL",
                 "net_income": "REAL", "eps": "REAL", "book_value": "REAL", "dividend_per_share": "REAL"}


def _column_defs(columns: list[str]) -> str:
    """Values are TEXT (dates as ISO strings, payment dates as JSON)
    unless numeric; every _source and _as_of column is TEXT."""
    return ", ".join(f"{c} {_COLUMN_TYPES.get(c, 'TEXT')}" for c in columns)


_TABLES = {
    "company_profiles": f"""
        CREATE TABLE company_profiles (
            symbol TEXT PRIMARY KEY,
            {_column_defs(_PROFILE_COLUMNS)},
            updated_at TEXT NOT NULL
        )""",
    "company_officers": """
        CREATE TABLE company_officers (
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            display_order INTEGER NOT NULL,
            source TEXT NOT NULL,
            as_of TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
    "company_financials": f"""
        CREATE TABLE company_financials (
            symbol TEXT NOT NULL,
            fiscal_year INTEGER NOT NULL,
            currency TEXT NOT NULL,
            {_column_defs(_FINANCIAL_COLUMNS)},
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, fiscal_year)
        )""",
}


def _to_db(value):
    if isinstance(value, list):  # dividend payment dates
        return json.dumps([v.isoformat() for v in value])
    if isinstance(value, date):
        return value.isoformat()
    return value


def _sourced_values(model, fields: Iterable[str]) -> tuple:
    out = []
    for f in fields:
        sourced = getattr(model, f)
        out += [_to_db(sourced.value), sourced.source, _to_db(sourced.as_of)]
    return tuple(out)


def _sourced_dict(row: dict, fields: Iterable[str]) -> dict:
    out = {}
    for f in fields:
        value = row[f]
        if f == "dividend_payment_dates" and value is not None:
            value = json.loads(value)
        out[f] = {"value": value, "source": row[f"{f}_source"], "as_of": row[f"{f}_as_of"]}
    return out


class CompanyStore:
    def __init__(self, db_path: "Path | str"):
        self._db_path = Path(db_path)
        conn = self._connect()
        try:
            for name, ddl in _TABLES.items():
                conn.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed(self, companies: Iterable[Company]) -> int:
        """Rebuild all three tables from `companies`, atomically. Returns
        the number of companies written."""
        updated_at = datetime.now(timezone.utc).isoformat()
        profiles, officers, financials = [], [], []
        for c in companies:
            profiles.append((c.symbol, *_sourced_values(c.profile, PROFILE_FIELDS), updated_at))
            officers += [
                (c.symbol, o.name, o.role, o.display_order, o.source, o.as_of.isoformat(), updated_at)
                for o in c.officers
            ]
            financials += [
                (c.symbol, f.fiscal_year, f.currency, *_sourced_values(f, FINANCIAL_FIELDS), updated_at)
                for f in c.financials
            ]
        conn = self._connect()
        # Explicit transaction: sqlite3's implicit one doesn't cover DDL.
        conn.isolation_level = None
        try:
            conn.execute("BEGIN")
            try:
                for name, ddl in _TABLES.items():
                    conn.execute(f"DROP TABLE IF EXISTS {name}")
                    conn.execute(ddl)
                conn.execute("CREATE INDEX company_officers_symbol ON company_officers (symbol)")
                self._insert(conn, "company_profiles", ["symbol", *_PROFILE_COLUMNS, "updated_at"], profiles)
                self._insert(conn, "company_officers", ["symbol", *_OFFICER_COLUMNS, "updated_at"], officers)
                self._insert(conn, "company_financials",
                             ["symbol", "fiscal_year", "currency", *_FINANCIAL_COLUMNS, "updated_at"], financials)
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()
        return len(profiles)

    @staticmethod
    def _insert(conn, table: str, columns: list[str], rows: list[tuple]) -> None:
        conn.executemany(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})", rows
        )

    def get(self, symbol: str) -> Optional[Company]:
        """Everything maintained about `symbol`; None if it has no entry.
        Officers come in display order, financials newest year first."""
        conn = self._connect()
        try:
            profile = conn.execute("SELECT * FROM company_profiles WHERE symbol = ?", (symbol,)).fetchone()
            if profile is None:
                return None
            officers = conn.execute(
                "SELECT * FROM company_officers WHERE symbol = ? ORDER BY display_order, name", (symbol,)
            ).fetchall()
            financials = conn.execute(
                "SELECT * FROM company_financials WHERE symbol = ? ORDER BY fiscal_year DESC", (symbol,)
            ).fetchall()
        finally:
            conn.close()
        return Company(
            symbol=symbol,
            profile=CompanyProfile(**_sourced_dict(dict(profile), PROFILE_FIELDS)),
            officers=[CompanyOfficer(**{c: row[c] for c in _OFFICER_COLUMNS}) for row in officers],
            financials=[
                FinancialYear(fiscal_year=row["fiscal_year"], currency=row["currency"],
                              **_sourced_dict(dict(row), FINANCIAL_FIELDS))
                for row in financials
            ],
        )
