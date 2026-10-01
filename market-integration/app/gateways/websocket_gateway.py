"""
Real-time delivery layer: per-client subscriptions over /ws/market.

Protocol (JSON text frames):

  server -> client, on connect
    {"type": "welcome", "symbols": [...], "asset_classes": {symbol: "equity" | "bill" | "bond"},
     "status": {...}}
                                                 what can be subscribed to, what each
                                                 is, and the market status; no prices
  client -> server
    {"action": "subscribe",   "symbols": [...]}
    {"action": "unsubscribe", "symbols": [...]}
  server -> client
    {"type": "subscribed",   "symbols": [newly added], "subscriptions": [all]}
    {"type": "snapshot",     "data": {symbol: quote}}   newly subscribed symbols only
    {"type": "unsubscribed", "symbols": [removed],     "subscriptions": [all]}
    {"type": "tick",         "data": quote}             subscribed symbols only
    {"type": "status", ...}                             every few seconds, to everyone:
                                                        session + feed state, same
                                                        body as GET /market/status
    {"type": "error", "code": "bad_json" | "bad_request" | "unknown_symbols",
     "message": ..., "symbols": [...]}

A client that never subscribes gets the welcome message and nothing
else. Unknown symbols in a request are reported in an error and the
valid ones are still applied. Symbols are case-insensitive.

Slow clients: broadcast() never awaits a send. Each client has its own
outbox -- control messages (acks, snapshots, errors) in order, plus at
most one pending tick per symbol and tick type -- drained by a per-client sender
task. A newer tick for a symbol replaces one that hasn't been sent yet
(conflation), so a slow client can't build a backlog or delay the feed
or anyone else; it just skips intermediate prices and catches up to the
current one. Status messages are conflated the same way: only the
newest unsent one is kept.

This is the same "latest per symbol" idea as
MarketDataBuffer.subscribe_latest(), but it deliberately lives here
rather than there: that buffer mode sits before validation and the
processor, so using it per client would hand browsers unvalidated
ticks and make every connection a separate buffer subscriber.
"""

import asyncio
import json
import logging
from collections import deque
from typing import Callable, Iterable, Mapping, Optional

from fastapi import WebSocket, WebSocketDisconnect

from app.models.tick import Tick

logger = logging.getLogger(__name__)


class _Client:
    """One connection: its subscriptions and its outbox."""

    __slots__ = (
        "ws", "subscriptions", "control", "pending", "status", "wakeup", "sender", "closed", "conflated",
    )

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.subscriptions: set[str] = set()
        self.control: deque[dict] = deque()     # sent in order, never dropped
        # (tick_type, symbol) -> latest unsent tick. Keyed by type too, so
        # a bond's repo tick can't replace its unsent quote, or vice versa.
        self.pending: dict[tuple[str, str], dict] = {}
        self.status: Optional[dict] = None      # latest unsent status message
        self.wakeup = asyncio.Event()
        self.sender: Optional[asyncio.Task] = None
        self.closed = False
        self.conflated = 0                      # ticks replaced before they were sent

    def send_control(self, message: dict) -> None:
        self.control.append(message)
        self.wakeup.set()

    def offer_status(self, message: dict) -> None:
        self.status = message
        self.wakeup.set()

    def offer_tick(self, key: tuple[str, str], message: dict) -> None:
        if key in self.pending:
            self.conflated += 1
        self.pending[key] = message
        self.wakeup.set()


class WebSocketGateway:

    def __init__(
        self,
        symbols: Iterable[str] | Callable[[], Iterable[str]],
        snapshot: Callable[[list[str]], Mapping[str, Tick]],
        status: Optional[Callable[[], dict]] = None,
        asset_class: Optional[Callable[[str], str]] = None,
    ):
        """`symbols` is what clients may subscribe to (the feed's
        universe), or a function returning it, for a universe that grows
        (T-bills are issued weekly); `snapshot` returns the latest quote
        for each of the given symbols that has one; `status`, if given,
        the current market status for the welcome message; `asset_class`,
        if given, what each symbol is, so a page can pick the ones it
        shows."""
        self._universe = symbols if callable(symbols) else (lambda fixed=list(symbols): fixed)
        self._asset_class = asset_class
        self._snapshot = snapshot
        self._status = status
        self._clients: dict[WebSocket, _Client] = {}
        self._subscribers: dict[str, set[_Client]] = {}

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def subscription_count(self) -> int:
        """Symbol subscriptions across all clients."""
        return sum(len(c.subscriptions) for c in self._clients.values())

    def subscribers(self, symbol: str) -> int:
        return len(self._subscribers.get(symbol, ()))

    # ------------------------------------------------------------ lifecycle

    async def serve(self, websocket: WebSocket) -> None:
        """Run one connection until the client goes away."""
        client = await self.connect(websocket)
        try:
            while True:
                self.handle_message(client, await websocket.receive_text())
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Market websocket connection failed")
        finally:
            await self.disconnect(websocket)

    async def connect(self, websocket: WebSocket) -> _Client:
        await websocket.accept()
        client = _Client(websocket)
        self._clients[websocket] = client
        client.sender = asyncio.create_task(self._send_loop(client))
        symbols = list(self._universe())
        welcome = {"type": "welcome", "symbols": symbols}
        if self._asset_class is not None:
            welcome["asset_classes"] = {s: self._asset_class(s) for s in symbols}
        if self._status is not None:
            welcome["status"] = self._status()
        client.send_control(welcome)
        logger.info("Client connected. Total clients: %d", len(self._clients))
        return client

    async def disconnect(self, websocket: WebSocket) -> None:
        client = self._clients.pop(websocket, None)
        if client is None:
            return
        self._drop(client)
        if client.sender is not None and client.sender is not asyncio.current_task():
            client.sender.cancel()
            try:
                await client.sender
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Client disconnected. Total clients: %d", len(self._clients))

    def _drop(self, client: _Client) -> None:
        """Remove a client from the symbol index (idempotent)."""
        client.closed = True
        for symbol in client.subscriptions:
            subs = self._subscribers.get(symbol)
            if subs is not None:
                subs.discard(client)
                if not subs:
                    del self._subscribers[symbol]
        client.subscriptions.clear()
        client.pending.clear()
        client.status = None

    # ------------------------------------------------------------ protocol

    def handle_message(self, client: _Client, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            client.send_control(_error("bad_json", "Message is not valid JSON."))
            return
        action = message.get("action") if isinstance(message, dict) else None
        symbols = message.get("symbols") if isinstance(message, dict) else None
        if action not in ("subscribe", "unsubscribe") or not (
            isinstance(symbols, list) and all(isinstance(s, str) for s in symbols)
        ):
            client.send_control(_error(
                "bad_request",
                'Expected {"action": "subscribe" | "unsubscribe", "symbols": [...]}.',
            ))
            return

        requested = list(dict.fromkeys(s.strip().upper() for s in symbols))
        known = set(self._universe())
        unknown = [s for s in requested if s not in known]
        valid = [s for s in requested if s in known]
        if unknown:
            client.send_control(_error(
                "unknown_symbols", "Not available on this feed: " + ", ".join(unknown), symbols=unknown,
            ))
        if action == "subscribe":
            self._subscribe(client, valid)
        else:
            self._unsubscribe(client, valid)

    def _subscribe(self, client: _Client, symbols: list[str]) -> None:
        added = [s for s in symbols if s not in client.subscriptions]
        for symbol in added:
            client.subscriptions.add(symbol)
            self._subscribers.setdefault(symbol, set()).add(client)
        client.send_control({
            "type": "subscribed", "symbols": added, "subscriptions": sorted(client.subscriptions),
        })
        if added:
            # Ticks that arrive from here on are queued behind this in the
            # outbox (control messages go first), so the snapshot always
            # reaches the client before any tick for these symbols.
            latest = self._snapshot(added)
            client.send_control({
                "type": "snapshot",
                "data": {s: latest[s].model_dump(mode="json") for s in added if s in latest},
            })

    def _unsubscribe(self, client: _Client, symbols: list[str]) -> None:
        removed = [s for s in symbols if s in client.subscriptions]
        gone = set(removed)
        client.pending = {k: m for k, m in client.pending.items() if k[1] not in gone}
        for symbol in removed:
            client.subscriptions.discard(symbol)
            subs = self._subscribers.get(symbol)
            if subs is not None:
                subs.discard(client)
                if not subs:
                    del self._subscribers[symbol]
        client.send_control({
            "type": "unsubscribed", "symbols": removed, "subscriptions": sorted(client.subscriptions),
        })

    # ------------------------------------------------------------ delivery

    async def broadcast(self, data: Tick) -> None:
        """Queue a tick for the symbol's subscribers. Never awaits a send,
        so no client can hold up the feed or the other clients. The
        message's data.tick_type tells clients what kind of tick it is."""
        subs = self._subscribers.get(data.symbol)
        if not subs:
            return
        message = {"type": "tick", "data": data.model_dump(mode="json")}
        for client in subs:
            client.offer_tick((data.tick_type, data.symbol), message)

    async def broadcast_status(self, status: dict) -> None:
        """Queue a market status message for every client, subscribed or
        not. Like broadcast(), never awaits a send."""
        message = {"type": "status", **status}
        for client in self._clients.values():
            if not client.closed:
                client.offer_status(message)

    async def _send_loop(self, client: _Client) -> None:
        try:
            while not client.closed:
                await client.wakeup.wait()
                client.wakeup.clear()
                while client.control:
                    await client.ws.send_json(client.control.popleft())
                if client.status is not None:
                    status, client.status = client.status, None
                    await client.ws.send_json(status)
                # Swap the dict out before sending: ticks that arrive while
                # we await a send land in the fresh one, not the one being
                # iterated.
                pending, client.pending = client.pending, {}
                for message in pending.values():
                    await client.ws.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The connection is gone or broken. Stop delivering to it; the
            # receive side of serve() notices and calls disconnect().
            logger.debug("Send to websocket client failed; dropping it", exc_info=True)
            self._drop(client)


def _error(code: str, message: str, symbols: Optional[list[str]] = None) -> dict:
    error = {"type": "error", "code": code, "message": message}
    if symbols is not None:
        error["symbols"] = symbols
    return error
