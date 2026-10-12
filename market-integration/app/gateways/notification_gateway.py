"""
Per-user notifications over /ws/notifications: ticket filed, ticket
rejected (app/tickets/desk.py).

Protocol (JSON text frames, server -> client only):

    {"type": "welcome", "user_id": ..., "unread": n}
    {"type": "notification", "notification": {...}}   as GET /notifications/me lists them

The user is identified as for HTTP requests (app/identity.py). A user may
have several connections (tabs, devices); each gets every notification.
Notifications are stored before they are pushed, so a user who wasn't
connected finds them at GET /notifications/me: the push is only for
seeing them at once.

Like the market gateway, push() never awaits a send: each connection
has its own queue and sender task, so a slow client can't hold up
whoever filed the ticket.
"""

import asyncio
import logging

from fastapi import WebSocket, WebSocketDisconnect

from app.models.ticket import Notification

logger = logging.getLogger(__name__)


class _Connection:
    __slots__ = ("ws", "user_id", "outbox", "sender")

    def __init__(self, ws: WebSocket, user_id: str):
        self.ws = ws
        self.user_id = user_id
        self.outbox: asyncio.Queue[dict] = asyncio.Queue()
        self.sender: "asyncio.Task | None" = None


class NotificationGateway:

    def __init__(self):
        self._connections: dict[str, set[_Connection]] = {}

    @property
    def client_count(self) -> int:
        return sum(len(c) for c in self._connections.values())

    async def serve(self, websocket: WebSocket, user_id: str, unread: int = 0) -> None:
        """Run one connection until the client goes away. Anything the
        client sends is ignored."""
        await websocket.accept()
        conn = _Connection(websocket, user_id)
        self._connections.setdefault(user_id, set()).add(conn)
        conn.sender = asyncio.create_task(self._send_loop(conn))
        conn.outbox.put_nowait({"type": "welcome", "user_id": user_id, "unread": unread})
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Notification websocket connection failed")
        finally:
            await self._drop(conn)

    def push(self, notification: Notification) -> None:
        """Queue a notification for every connection of its user."""
        message = {"type": "notification", "notification": notification.model_dump(mode="json")}
        for conn in self._connections.get(notification.user_id, ()):
            conn.outbox.put_nowait(message)

    async def _send_loop(self, conn: _Connection) -> None:
        try:
            while True:
                await conn.ws.send_json(await conn.outbox.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Send to notification client failed; dropping it", exc_info=True)
            self._forget(conn)

    def _forget(self, conn: _Connection) -> None:
        conns = self._connections.get(conn.user_id)
        if conns is not None:
            conns.discard(conn)
            if not conns:
                del self._connections[conn.user_id]

    async def _drop(self, conn: _Connection) -> None:
        self._forget(conn)
        if conn.sender is not None:
            conn.sender.cancel()
            try:
                await conn.sender
            except (asyncio.CancelledError, Exception):
                pass
