"""
SQLite storage for trade tickets and the notifications sent about them.

User data, like watchlists: created if missing and never rebuilt. Each
ticket is stored whole as JSON, with the columns queries need beside
it. Status changes go through transition(), which only moves a ticket
that is still in the expected state, so two requests can't both file
the same ticket.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.models.ticket import Notification, NotificationKind, Ticket, TicketStatus

_CREATE_TABLES = (
    """
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        trader TEXT NOT NULL,
        counterparty TEXT NOT NULL,
        created_at TEXT NOT NULL,
        body TEXT NOT NULL          -- the Ticket as JSON
    )
    """,
    "CREATE INDEX IF NOT EXISTS tickets_trader ON tickets (trader)",
    "CREATE INDEX IF NOT EXISTS tickets_counterparty ON tickets (counterparty)",
    """
    CREATE TABLE IF NOT EXISTS ticket_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        ticket_id TEXT NOT NULL,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        read INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS ticket_notifications_user ON ticket_notifications (user_id)",
)

# Computed on the model, so not stored.
_DERIVED = {"buyer", "seller"}


class TicketStore:
    def __init__(self, db_path: "Path | str"):
        self._db_path = Path(db_path)
        conn = self._connect()
        try:
            for statement in _CREATE_TABLES:
                conn.execute(statement)
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    # ------------------------------------------------------------ tickets

    def add(self, ticket: Ticket) -> Ticket:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO tickets (id, status, trader, counterparty, created_at, body) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket.id, ticket.status, ticket.trader, ticket.counterparty,
                 ticket.created_at.isoformat(), ticket.model_dump_json(exclude=_DERIVED)),
            )
            conn.commit()
        finally:
            conn.close()
        return ticket

    def get(self, ticket_id: str) -> Optional[Ticket]:
        conn = self._connect()
        try:
            row = conn.execute("SELECT body FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        finally:
            conn.close()
        return Ticket.model_validate_json(row[0]) if row else None

    def for_user(self, user_id: str, status: Optional[TicketStatus] = None) -> list[Ticket]:
        """Tickets the user is a party to, either side, newest first."""
        query = "SELECT body FROM tickets WHERE (trader = ? OR counterparty = ?)"
        params: list = [user_id, user_id]
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [Ticket.model_validate_json(r[0]) for r in rows]

    def transition(self, ticket_id: str, expected: TicketStatus, **changes) -> Optional[Ticket]:
        """Apply `changes` (a new status and anything else) to the ticket,
        but only if its status is still `expected`. The updated ticket,
        or None if it isn't there or has moved on."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT body FROM tickets WHERE id = ? AND status = ?", (ticket_id, expected)
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            ticket = Ticket.model_validate_json(row[0]).model_copy(
                update={**changes, "updated_at": datetime.now(timezone.utc)}
            )
            conn.execute(
                "UPDATE tickets SET status = ?, body = ? WHERE id = ?",
                (ticket.status, ticket.model_dump_json(exclude=_DERIVED), ticket_id),
            )
            conn.commit()
        finally:
            conn.close()
        return ticket

    # ------------------------------------------------------------ notifications

    def notify(self, user_id: str, kind: NotificationKind, ticket_id: str, message: str) -> Notification:
        created_at = datetime.now(timezone.utc)
        conn = self._connect()
        try:
            cursor = conn.execute(
                "INSERT INTO ticket_notifications (user_id, kind, ticket_id, message, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (user_id, kind, ticket_id, message, created_at.isoformat()),
            )
            conn.commit()
            notification_id = cursor.lastrowid
        finally:
            conn.close()
        return Notification(
            id=notification_id, user_id=user_id, kind=kind, ticket_id=ticket_id,
            message=message, created_at=created_at,
        )

    def notifications(self, user_id: str, unread_only: bool = False) -> list[Notification]:
        """The user's notifications, newest first."""
        query = (
            "SELECT id, user_id, kind, ticket_id, message, created_at, read "
            "FROM ticket_notifications WHERE user_id = ?"
        )
        if unread_only:
            query += " AND read = 0"
        query += " ORDER BY id DESC"
        conn = self._connect()
        try:
            rows = conn.execute(query, (user_id,)).fetchall()
        finally:
            conn.close()
        return [
            Notification(
                id=r[0], user_id=r[1], kind=r[2], ticket_id=r[3], message=r[4],
                created_at=datetime.fromisoformat(r[5]), read=bool(r[6]),
            )
            for r in rows
        ]

    def mark_read(self, user_id: str, notification_id: int) -> bool:
        """Mark one of the user's notifications read. False if they have
        no notification with that ID."""
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE ticket_notifications SET read = 1 WHERE id = ? AND user_id = ?",
                (notification_id, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
