"""
SQLite storage for what the economic calendar pulls: BoG's policy rate
decisions and GSS's release calendar, as published. Kept across
restarts, so the calendar works offline with what was last pulled.

Policy decisions are only ever added or corrected (keyed by date). GSS's
calendar is replaced a year at a time: when GSS moves a release to
another week its id changes ("2026-10-cpi-w2" -> "-w3"), so upserting
would leave the old week behind as a phantom release.
"""

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable

from app.econ import GssRelease, PolicyDecision

_CREATE = (
    """
    CREATE TABLE IF NOT EXISTS policy_rate_decisions (
        date TEXT PRIMARY KEY,        -- YYYY-MM-DD, the announcement
        rate REAL NOT NULL,           -- % a year
        fetched_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gss_releases (
        id TEXT PRIMARY KEY,          -- GSS's own
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        week INTEGER,                 -- of the month; NULL where GSS gives none
        title TEXT NOT NULL,
        short_code TEXT,
        frequency TEXT NOT NULL,
        category TEXT NOT NULL,
        reference_period TEXT,
        status TEXT NOT NULL,
        needs_verification INTEGER NOT NULL,
        source_note TEXT,
        fetched_at TEXT NOT NULL
    )
    """,
)


class EconStore:
    def __init__(self, db_path: "Path | str"):
        self._db_path = Path(db_path)
        conn = self._connect()
        try:
            for sql in _CREATE:
                conn.execute(sql)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def upsert_decisions(self, decisions: Iterable[PolicyDecision]) -> None:
        fetched_at = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            conn.executemany(
                "INSERT INTO policy_rate_decisions (date, rate, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET rate = excluded.rate",
                [(d.date.isoformat(), d.rate, fetched_at) for d in decisions],
            )
            conn.commit()
        finally:
            conn.close()

    def decisions(self) -> list[PolicyDecision]:
        """Oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT date, rate FROM policy_rate_decisions ORDER BY date").fetchall()
        finally:
            conn.close()
        return [PolicyDecision(date.fromisoformat(d), r) for d, r in rows]

    def replace_releases(self, releases: list[GssRelease]) -> None:
        """Make the stored calendar for each year in `releases` exactly
        `releases`; other years are left alone."""
        fetched_at = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            with conn:
                for year in {r.year for r in releases}:
                    conn.execute("DELETE FROM gss_releases WHERE year = ?", (year,))
                conn.executemany(
                    "INSERT OR REPLACE INTO gss_releases (id, year, month, week, title, short_code, frequency, "
                    "category, reference_period, status, needs_verification, source_note, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(r.id, r.year, r.month, r.week, r.title, r.short_code, r.frequency, r.category,
                      r.reference_period, r.status, int(r.needs_verification), r.source_note, fetched_at)
                     for r in releases],
                )
        finally:
            conn.close()

    def releases(self) -> list[GssRelease]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, year, month, week, title, short_code, frequency, category, reference_period, "
                "status, needs_verification, source_note FROM gss_releases ORDER BY year, month, week, id"
            ).fetchall()
        finally:
            conn.close()
        return [GssRelease(i, y, m, w, t, sc, f, c, rp, s, bool(nv), sn)
                for i, y, m, w, t, sc, f, c, rp, s, nv, sn in rows]
