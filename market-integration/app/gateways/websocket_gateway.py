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
    {"type": "ticks",        "data": [quote, ...]}      subscribed symbols only: the latest
                                                        quote for each that changed, at
                                                        most once per send interval
    {"type": "status", ...}                             every few seconds, to everyone:
                                                        session + feed state, same
                                                        body as GET /market/status
    {"type": "error", "code": "bad_json" | "bad_request" | "unknown_symbols",
     "message": ..., "symbols": [...]}

A client that never subscribes gets the welcome message and nothing
else. Unknown symbols in a request are reported in an error and the
valid ones are still applied. Symbols are case-insensitive.

Throttling and conflation (#20): a person can't read more than a few
updates a second per instrument, so ticks aren't sent one by one. Each
client has its own outbox -- control messages (acks, snapshots, errors)
in order, plus at most one pending tick per symbol and tick type --
drained by a per-client sender task. A newer tick for a symbol replaces
one that hasn't been sent yet (conflation), and the pending ticks go out
together as one "ticks" message at most once per `send_interval`. So
however fast the feed runs, a client gets a bounded number of messages a
second (1 / send_interval, plus the occasional control or status
message), each holding at most one quote per subscribed symbol.

Control messages don't wait for the interval: an ack or snapshot goes
at once, and always before any ticks queued after it, so a snapshot
reaches the client before ticks for the symbols it covers. Status
messages are conflated too: only the newest unsent one is kept.

broadcast() never awaits a send, so a slow client can't build a backlog
or delay the feed or anyone else; it skips intermediate prices and
catches up to the current one.

Upstream, the processor branch reads the buffer conflated as well
(MarketDataBuffer.subscribe_latest(), see app/main.py), so a tick burst
costs the display path one validation per symbol per read, not one per
tick. The outbox conflation still lives here, per client: clients drain
at their own pace, and the buffer sits before validation, so browsers
never see an unvalidated tick.
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
        "ws", "subscriptions", "control", "pending", "status", "wakeup", "urgent", "next_batch",
        "sender", "closed", "conflated",
    )

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.subscriptions: set[str] = set()
        self.control: deque[dict] = deque()     # sent in order, never dropped
        # (tick_type, symbol) -> latest unsent tick. Keyed by type too, so
        # a bond's repo tick can't replace its unsent quote, or vice versa.
        self.pending: dict[tuple[str, str], Tick] = {}
        self.status: Optional[dict] = None      # latest unsent status message
        self.wakeup = asyncio.Event()           # anything to send
        # Control or status to send: these cut short the wait for the next
        # tick batch; a tick doesn't, so a fast feed can't spin the sender.
        self.urgent = asyncio.Event()
        self.next_batch = 0.0                   # loop time the next tick batch may go
        self.sender: Optional[asyncio.Task] = None
        self.closed = False
        self.conflated = 0                      # ticks replaced before they were sent

    def send_control(self, message: dict) -> None:
        self.control.append(message)
        self.urgent.set()
        self.wakeup.set()

    def offer_status(self, message: dict) -> None:
        self.status = message
        self.urgent.set()
        self.wakeup.set()

    def offer_tick(self, key: tuple[str, str], tick: Tick) -> None:
        if key in self.pending:
            self.conflated += 1
        self.pending[key] = tick
        self.wakeup.set()


class WebSocketGateway:

    def __init__(
        self,
        symbols: Iterable[str] | Callable[[], Iterable[str]],
        snapshot: Callable[[list[str]], Mapping[str, Tick]],
        status: Optional[Callable[[], dict]] = None,
        asset_class: Optional[Callable[[str], str]] = None,
        send_interval: float = 0.0,
    ):
        """`symbols` is what clients may subscribe to (the feed's
        universe), or a function returning it, for a universe that grows
        (T-bills are issued weekly); `snapshot` returns the latest quote
        for each of the given symbols that has one; `status`, if given,
        the current market status for the welcome message; `asset_class`,
        if given, what each symbol is, so a page can pick the ones it
        shows; `send_interval`, the least time in seconds between two
        tick messages to one client (0: send as soon as there's news)."""
        self._universe = symbols if callable(symbols) else (lambda fixed=list(symbols): fixed)
        self._asset_class = asset_class
        self._snapshot = snapshot
        self._status = status
        self.send_interval = send_interval
        self._clients: dict[WebSocket, _Client] = {}
        self._subscribers: dict[str, set[_Client]] = {}
        self._conflated_closed = 0  # conflated ticks of clients since gone
        # Each tick's JSON form, made once however many clients it goes
        # to, and only for ticks actually sent: (tick_type, symbol) ->
        # (tick, its dump).
        self._dumped: dict[tuple[str, str], tuple[Tick, dict]] = {}
        self.messages_sent = 0      # frames of any type, to all clients ever
        self.tick_messages_sent = 0 # "ticks" frames among them
        self.ticks_sent = 0         # quotes inside those frames

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def conflated_count(self) -> int:
        """Ticks never sent because a newer one for the same symbol
        replaced them in a slow client's outbox, across all clients ever."""
        return self._conflated_closed + sum(c.conflated for c in self._clients.values())

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
        self._conflated_closed += client.conflated
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
        """Queue a tick for the symbol's subscribers, replacing any unsent
        one for the same symbol. Never awaits a send, so no client can
        hold up the feed or the other clients. Each quote's tick_type
        tells clients what kind of tick it is."""
        subs = self._subscribers.get(data.symbol)
        if not subs:
            return
        key = (data.tick_type, data.symbol)
        for client in subs:
            client.offer_tick(key, data)

    async def broadcast_status(self, status: dict) -> None:
        """Queue a market status message for every client, subscribed or
        not. Like broadcast(), never awaits a send."""
        message = {"type": "status", **status}
        for client in self._clients.values():
            if not client.closed:
                client.offer_status(message)

    async def _send_loop(self, client: _Client) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not client.closed:
                await client.wakeup.wait()
                client.wakeup.clear()
                client.urgent.clear()
                await self._send_control(client)
                # Hold ticks until this client's interval is up. Control and
                # status that arrive meanwhile still go at once; ticks just
                # replace each other in the outbox.
                while client.pending and not client.closed:
                    delay = client.next_batch - loop.time()
                    if delay <= 0:
                        break
                    try:
                        await asyncio.wait_for(client.urgent.wait(), delay)
                    except asyncio.TimeoutError:
                        break
                    client.urgent.clear()
                    await self._send_control(client)
                if not client.pending or client.closed:
                    continue
                # Swap the dict out before sending: ticks that arrive while
                # we await the send land in the fresh one for next time.
                pending, client.pending = client.pending, {}
                await self._send(client, {"type": "ticks", "data": [self._dump(t) for t in pending.values()]})
                self.tick_messages_sent += 1
                self.ticks_sent += len(pending)
                client.next_batch = loop.time() + self.send_interval
        except asyncio.CancelledError:
            raise
        except Exception:
            # The connection is gone or broken. Stop delivering to it; the
            # receive side of serve() notices and calls disconnect().
            logger.debug("Send to websocket client failed; dropping it", exc_info=True)
            self._drop(client)

    async def _send_control(self, client: _Client) -> None:
        """Send queued control messages in order, then the latest status."""
        while client.control:
            await self._send(client, client.control.popleft())
        if client.status is not None:
            status, client.status = client.status, None
            await self._send(client, status)

    async def _send(self, client: _Client, message: dict) -> None:
        await client.ws.send_json(message)
        self.messages_sent += 1

    def _dump(self, tick: Tick) -> dict:
        key = (tick.tick_type, tick.symbol)
        cached = self._dumped.get(key)
        if cached is None or cached[0] is not tick:
            cached = (tick, tick.model_dump(mode="json"))
            self._dumped[key] = cached
        return cached[1]


def _error(code: str, message: str, symbols: Optional[list[str]] = None) -> dict:
    error = {"type": "error", "code": code, "message": message}
    if symbols is not None:
        error["symbols"] = symbols
    return error
