"""
Async buffer/broadcaster sitting between a market data connector's live
stream and the downstream consumers (processor, gateway, Symphony
delivery, ...).

Supports any number of independent subscribers, each in one of two modes:

  * subscribe()          -- ordered replay. A bounded per-subscriber FIFO
                             of raw ticks (all symbols interleaved in
                             arrival order). If the subscriber falls
                             behind and the queue fills up, the oldest
                             buffered tick is dropped to make room. This
                             preserves ordering and every symbol gets a
                             fair share of the buffer, but under
                             sustained backpressure a slow consumer can
                             still miss whole rounds for some symbols --
                             the drop is chronological, not per-symbol.

  * subscribe_latest()    -- conflated. Only the single most recent tick
                             per symbol is kept per subscriber; a new
                             tick for a symbol overwrites any unread one
                             for that same symbol instead of queueing
                             behind it. A slow consumer never falls
                             permanently behind and never misses a
                             symbol -- it just always catches up to the
                             current price for each one. This is the
                             right mode for anything that only cares
                             about "what's the price right now" (a
                             gateway/dashboard), as opposed to
                             "processor" reads if it needs to see every
                             tick, use subscribe() instead.

Usage:

    connector = MockMarketConnector(symbols=["AAPL", "TSLA"])
    await connector.connect()

    buffer = MarketDataBuffer(connector.stream(), maxsize=200)
    await buffer.start()

    processor_feed = buffer.subscribe("processor")        # every tick
    gateway_feed = buffer.subscribe_latest("gateway")      # latest per symbol

    async for tick in gateway_feed:
        ...

    await buffer.stop()
"""

import asyncio
import logging
from typing import AsyncIterator, Optional

from app.models.market_data import MarketData

logger = logging.getLogger(__name__)


class _ConflatedSubscriber:
    """Per-subscriber state for subscribe_latest(): at most one unread
    tick per symbol, plus an event to wake the consumer when something
    new has landed."""

    __slots__ = ("latest", "pending", "event")

    def __init__(self):
        self.latest: dict[str, MarketData] = {}
        self.pending: set[str] = set()
        self.event = asyncio.Event()


class MarketDataBuffer:
    def __init__(self, source: AsyncIterator[MarketData], maxsize: int = 200):
        self._source = source
        self._maxsize = maxsize
        self._subscribers: dict[str, "asyncio.Queue[MarketData]"] = {}
        self._conflated: dict[str, _ConflatedSubscriber] = {}
        self._dropped_counts: dict[str, int] = {}
        self._pump_task: Optional[asyncio.Task] = None
        self._running = False

    def subscribe(self, name: str) -> AsyncIterator[MarketData]:
        """Register a consumer that wants every tick, in order.

        `name` just needs to be unique across BOTH subscribe() and
        subscribe_latest() -- it's used for logging/metrics and for
        unsubscribe().
        """
        self._check_name_free(name)

        queue: "asyncio.Queue[MarketData]" = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers[name] = queue
        self._dropped_counts[name] = 0
        return self._consume(name, queue)

    def subscribe_latest(self, name: str) -> AsyncIterator[MarketData]:
        """Register a consumer that only wants the current price per
        symbol (conflated). Never falls behind, never misses a symbol,
        but intermediate ticks between reads are lost by design.
        """
        self._check_name_free(name)

        sub = _ConflatedSubscriber()
        self._conflated[name] = sub
        return self._consume_latest(name, sub)

    def _check_name_free(self, name: str) -> None:
        if name in self._subscribers or name in self._conflated:
            raise ValueError(f"Subscriber '{name}' is already registered.")

    def unsubscribe(self, name: str) -> None:
        self._subscribers.pop(name, None)
        self._conflated.pop(name, None)
        self._dropped_counts.pop(name, None)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._pump_task = asyncio.create_task(self._pump())

    async def stop(self) -> None:
        self._running = False
        if self._pump_task:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None

    @property
    def dropped_counts(self) -> dict[str, int]:
        """How many ticks each subscriber has lost to backpressure so far."""
        return dict(self._dropped_counts)

    @property
    def healthy(self) -> bool:
        """False once start() hasn't run yet, or the pump task has
        stopped or crashed (e.g. the source connector raised)."""
        return self._pump_task is not None and not self._pump_task.done()

    async def _pump(self) -> None:
        try:
            async for tick in self._source:
                for name, queue in list(self._subscribers.items()):
                    self._offer(name, queue, tick)
                for name, sub in list(self._conflated.items()):
                    self._offer_latest(sub, tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Market data buffer pump crashed")
            raise

    def _offer(self, name: str, queue: "asyncio.Queue[MarketData]", tick: MarketData) -> None:
        try:
            queue.put_nowait(tick)
            return
        except asyncio.QueueFull:
            pass

        # Drop the oldest buffered tick to make room, then retry once.
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._dropped_counts[name] = self._dropped_counts.get(name, 0) + 1

        try:
            queue.put_nowait(tick)
        except asyncio.QueueFull:
            # Another producer beat us to the freed slot; skip this tick.
            pass

    async def _consume(self, name: str, queue: "asyncio.Queue[MarketData]") -> AsyncIterator[MarketData]:
        try:
            while True:
                tick = await queue.get()
                yield tick
        finally:
            self.unsubscribe(name)

    def _offer_latest(self, sub: _ConflatedSubscriber, tick: MarketData) -> None:
        # Overwrite (not queue behind) any unread tick for this symbol --
        # this is what makes conflation immune to backpressure: storage
        # per subscriber is bounded by symbol count, never by feed rate.
        sub.latest[tick.symbol] = tick
        sub.pending.add(tick.symbol)
        sub.event.set()

    async def _consume_latest(self, name: str, sub: _ConflatedSubscriber) -> AsyncIterator[MarketData]:
        try:
            while True:
                await sub.event.wait()
                # Snapshot + clear before yielding so ticks that land
                # while we're yielding aren't lost.
                symbols = list(sub.pending)
                sub.pending.clear()
                sub.event.clear()
                for symbol in symbols:
                    yield sub.latest[symbol]
        finally:
            self.unsubscribe(name)
