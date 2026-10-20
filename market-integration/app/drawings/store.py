"""
SQLite storage for chart drawings: one row per user per symbol, the
drawings as a JSON array (app/models/drawing.py's shape, validated before
it gets here).

User data, like watchlists: created if missing and never rebuilt, so
drawings survive restarts.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS chart_drawings (
        user_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        drawings TEXT NOT NULL,     -- JSON array
        updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, symbol)
    )
"""


class DrawingStore:
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

    def get(self, user_id: str, symbol: str) -> Optional[dict]:
        """{"drawings": [...], "updated_at": iso} if the user has saved any
        for `symbol`, else None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT drawings, updated_at FROM chart_drawings WHERE user_id = ? AND symbol = ?",
                (user_id, symbol),
            ).fetchone()
        finally:
            conn.close()
        return {"drawings": json.loads(row[0]), "updated_at": row[1]} if row else None

    def put(self, user_id: str, symbol: str, drawings: list[dict]) -> dict:
        """Replace the user's drawings on `symbol`. An empty list removes
        the row."""
        updated_at = datetime.now(timezone.utc).isoformat()
        conn = self._connect()
        try:
            if drawings:
                conn.execute(
                    "INSERT INTO chart_drawings (user_id, symbol, drawings, updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(user_id, symbol) DO UPDATE SET drawings = excluded.drawings, "
                    "updated_at = excluded.updated_at",
                    (user_id, symbol, json.dumps(drawings), updated_at),
                )
            else:
                conn.execute("DELETE FROM chart_drawings WHERE user_id = ? AND symbol = ?", (user_id, symbol))
            conn.commit()
        finally:
            conn.close()
        return {"drawings": list(drawings), "updated_at": updated_at}

    def symbols(self, user_id: str) -> dict[str, int]:
        """symbol -> how many drawings, for each chart the user has drawn on."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT symbol, drawings FROM chart_drawings WHERE user_id = ? ORDER BY symbol", (user_id,)
            ).fetchall()
        finally:
            conn.close()
        return {s: len(json.loads(d)) for s, d in rows}
