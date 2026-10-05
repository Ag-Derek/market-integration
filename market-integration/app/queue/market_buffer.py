"""
Async buffer/broadcaster sitting between a market data connector's live
stream and the downstream consumers (processor, gateway, Symphony
delivery, ...).

Supports any number of independent subscribers, each in one of three
modes:

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
                             Fine for a live display, which only shows
                             the latest price anyway.

  * subscribe(lossless=True)
                          -- ordered replay for consumers that must see
                             every tick (the candle aggregator, alerts):
                             a missed tick can mean a wrong candle high,
                             low or volume, or a missed crossing. The
                             queue is much larger (lossless_maxsize), and
                             when it is full anyway the pump waits up to
                             block_timeout for room -- pausing the feed
                             for everyone, briefly -- before it falls
                             back to dropping the oldest tick. Such a
                             drop is logged, counted, and handed to the
                             subscriber's on_drop callback, so it can
                             mark what it built from the gap (e.g.
                             candles as possibly incomplete). A
                             subscriber whose wait timed out doesn't
                             block the feed again until it has caught up
                             enough to have room, so a stalled or dead
                             consumer costs one timeout, not one per
                             tick.

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
                             tick, use subscribe() instead. The app's
                             display branch (processor -> WebSocket
                             gateway) reads this way (#20).

Usage:

    connector = MockMarketConnector(symbols=["MTNGH", "GCB"])
    await connector.connect()

    buffer = MarketDataBuffer(connector.stream(), maxsize=200)
    await buffer.start()

    candle_feed = buffer.subscribe("aggregator", lossless=True)  # every tick
    display_feed = buffer.subscribe_latest("processor")          # latest per symbol

    async for tick in display_feed:
        ...

    await buffer.stop()
"""

import asyncio
import logging
import time
from typing import AsyncIterator, Callable, Optional

from app.models.tick import Tick

logger = logging.getLogger(__name__)

# How often, at most, a lossless subscriber's drops are logged; the
# count since the last log line goes with it.
DROP_LOG_INTERVAL_SECONDS = 10.0


class _QueuedSubscriber:
    """Per-subscriber state for subscribe()."""

    __slots__ = (
        "queue", "lossless", "on_drop", "dropped", "blocked_seconds", "peak_depth",
        "stalled", "unlogged_drops", "last_drop_log",
    )

    def __init__(self, maxsize: int, lossless: bool, on_drop: Optional[Callable[[Tick], None]]):
        self.queue: "asyncio.Queue[Tick]" = asyncio.Queue(maxsize=maxsize)
        self.lossless = lossless
        self.on_drop = on_drop
        self.dropped = 0
        # Time the pump has spent waiting for this subscriber to make room.
        self.blocked_seconds = 0.0
        # Most ticks ever waiting at once: how close it came to full.
        self.peak_depth = 0
        # Set when a wait for room timed out; cleared once there's room
        # again. While set, a full queue drops instead of blocking.
        self.stalled = False
        self.unlogged_drops = 0
        self.last_drop_log = float("-inf")


class _ConflatedSubscriber:
    """Per-subscriber state for subscribe_latest(): at most one unread
    tick per symbol and tick type (so a bond's repo tick never replaces
    its quote), plus an event to wake the consumer when something new
    has landed."""

    __slots__ = ("latest", "pending", "event", "conflated")

    def __init__(self):
        self.latest: dict[tuple[str, str], Tick] = {}
        self.pending: set[tuple[str, str]] = set()
        self.event = asyncio.Event()
        self.conflated = 0  # ticks overwritten by a newer one before they were read


class MarketDataBuffer:
    def __init__(
        self,
        source: AsyncIterator[Tick],
        maxsize: int = 200,
        lossless_maxsize: int = 10_000,
        block_timeout: float = 0.5,
    ):
        self._source = source
        self._maxsize = maxsize
        self._lossless_maxsize = lossless_maxsize
        self._block_timeout = block_timeout
        self._subscribers: dict[str, _QueuedSubscriber] = {}
        self._conflated: dict[str, _ConflatedSubscriber] = {}
        self.received = 0  # ticks taken off the source, ever
        self._pump_task: Optional[asyncio.Task] = None
        self._running = False

    def subscribe(
        self,
        name: str,
        *,
        lossless: bool = False,
        on_drop: Optional[Callable[[Tick], None]] = None,
    ) -> AsyncIterator[Tick]:
        """Register a consumer that wants every tick, in order.

        `name` just needs to be unique across BOTH subscribe() and
        subscribe_latest() -- it's used for logging/metrics and for
        unsubscribe().

        `lossless` gives it the large queue and block-before-drop policy
        described in the module docstring. `on_drop` is called with each
        tick the subscriber loses, synchronously, before the tick after
        it is queued: so it's always the oldest unread tick, the very
        next one the subscriber would have read.
        """
        self._check_name_free(name)

        sub = _QueuedSubscriber(
            self._lossless_maxsize if lossless else self._maxsize, lossless, on_drop
        )
        self._subscribers[name] = sub
        return self._consume(name, sub.queue)

    def subscribe_latest(self, name: str) -> AsyncIterator[Tick]:
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
        """How many ticks each subscribe() subscriber has lost to
        backpressure so far."""
        return {name: sub.dropped for name, sub in self._subscribers.items()}

    def stats(self) -> list[dict]:
        """Per subscribe() subscriber: its policy, queue depth (now and
        the most ever) and capacity, ticks dropped, and seconds the pump
        spent waiting on it. For /metrics."""
        return [
            {
                "subscriber": name,
                "policy": "lossless" if sub.lossless else "drop_oldest",
                "depth": sub.queue.qsize(),
                "peak_depth": sub.peak_depth,
                "capacity": sub.queue.maxsize,
                "dropped": sub.dropped,
                "blocked_seconds": sub.blocked_seconds,
            }
            for name, sub in self._subscribers.items()
        ]

    @property
    def conflated_counts(self) -> dict[str, int]:
        """How many ticks each subscribe_latest() subscriber never read
        because a newer one for the same symbol replaced them: expected,
        and the point of conflation, not a loss."""
        return {name: sub.conflated for name, sub in self._conflated.items()}

    @property
    def maxsize(self) -> int:
        """Queue size of a plain (drop-oldest) subscribe() subscriber."""
        return self._maxsize

    @property
    def queue_depths(self) -> dict[str, int]:
        """Ticks waiting to be read, per subscriber: queued ticks for
        subscribe(), symbols with an unread tick for subscribe_latest()."""
        depths = {name: sub.queue.qsize() for name, sub in self._subscribers.items()}
        depths.update({name: len(sub.pending) for name, sub in self._conflated.items()})
        return depths

    @property
    def subscriber_modes(self) -> dict[str, str]:
        """'queue' (subscribe(), drop-oldest), 'lossless'
        (subscribe(lossless=True)) or 'latest' (subscribe_latest()), per
        subscriber."""
        modes = {name: "lossless" if sub.lossless else "queue" for name, sub in self._subscribers.items()}
        modes.update({name: "latest" for name in self._conflated})
        return modes

    @property
    def healthy(self) -> bool:
        """False once start() hasn't run yet, or the pump task has
        stopped or crashed (e.g. the source connector raised)."""
        return self._pump_task is not None and not self._pump_task.done()

    async def _pump(self) -> None:
        try:
            async for tick in self._source:
                self.received += 1
                waiting = []
                for name, sub in list(self._subscribers.items()):
                    if not self._offer(name, sub, tick):
                        waiting.append((name, sub))
                for sub in list(self._conflated.values()):
                    self._offer_latest(sub, tick)
                # Everyone else has this tick before the feed pauses for
                # a full lossless subscriber.
                for name, sub in waiting:
                    await self._offer_waiting(name, sub, tick)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Market data buffer pump crashed")
            raise

    def _offer(self, name: str, sub: _QueuedSubscriber, tick: Tick) -> bool:
        """Queue `tick` without waiting. False if a lossless subscriber's
        queue is full and the pump should wait for room."""
        try:
            sub.queue.put_nowait(tick)
            sub.stalled = False
            sub.peak_depth = max(sub.peak_depth, sub.queue.qsize())
            return True
        except asyncio.QueueFull:
            pass
        if sub.lossless and not sub.stalled and self._block_timeout > 0:
            return False
        self._drop_oldest_and_put(name, sub, tick)
        return True

    async def _offer_waiting(self, name: str, sub: _QueuedSubscriber, tick: Tick) -> None:
        # perf_counter: monotonic() ticks in ~15 ms steps on Windows, and
        # most waits are shorter than that.
        start = time.perf_counter()
        try:
            await asyncio.wait_for(sub.queue.put(tick), self._block_timeout)
            sub.peak_depth = max(sub.peak_depth, sub.queue.qsize())
            return
        except asyncio.TimeoutError:
            pass
        finally:
            sub.blocked_seconds += time.perf_counter() - start
        if self._subscribers.get(name) is not sub:
            return  # unsubscribed while we waited
        sub.stalled = True
        self._drop_oldest_and_put(name, sub, tick)

    def _drop_oldest_and_put(self, name: str, sub: _QueuedSubscriber, tick: Tick) -> None:
        try:
            dropped = sub.queue.get_nowait()
        except asyncio.QueueEmpty:
            dropped = None
        if dropped is not None:
            self._dropped(name, sub, dropped)

        try:
            sub.queue.put_nowait(tick)
            sub.peak_depth = max(sub.peak_depth, sub.queue.qsize())
        except asyncio.QueueFull:
            # Another producer beat us to the freed slot; skip this tick.
            self._dropped(name, sub, tick)

    def _dropped(self, name: str, sub: _QueuedSubscriber, tick: Tick) -> None:
        sub.dropped += 1
        if sub.on_drop is not None:
            try:
                sub.on_drop(tick)
            except Exception:
                logger.exception("on_drop callback for subscriber '%s' failed", name)
        if not sub.lossless:
            return  # expected under load for a display branch
        sub.unlogged_drops += 1
        now = time.monotonic()
        if now - sub.last_drop_log >= DROP_LOG_INTERVAL_SECONDS:
            logger.warning(
                "Lossless subscriber '%s' dropped %d tick(s) (%d in total): its queue "
                "of %d stayed full for over %.2fs",
                name, sub.unlogged_drops, sub.dropped, sub.queue.maxsize, self._block_timeout,
            )
            sub.unlogged_drops = 0
            sub.last_drop_log = now

    async def _consume(self, name: str, queue: "asyncio.Queue[Tick]") -> AsyncIterator[Tick]:
        try:
            while True:
                tick = await queue.get()
                yield tick
        finally:
            self.unsubscribe(name)

    def _offer_latest(self, sub: _ConflatedSubscriber, tick: Tick) -> None:
        # Overwrite (not queue behind) any unread tick for this symbol --
        # this is what makes conflation immune to backpressure: storage
        # per subscriber is bounded by symbol count, never by feed rate.
        key = (tick.tick_type, tick.symbol)
        if key in sub.pending:
            sub.conflated += 1
        sub.latest[key] = tick
        sub.pending.add(key)
        sub.event.set()

    async def _consume_latest(self, name: str, sub: _ConflatedSubscriber) -> AsyncIterator[Tick]:
        try:
            while True:
                await sub.event.wait()
                # Snapshot + clear before yielding so ticks that land
                # while we're yielding aren't lost.
                keys = list(sub.pending)
                sub.pending.clear()
                sub.event.clear()
                for key in keys:
                    yield sub.latest[key]
        finally:
            self.unsubscribe(name)
