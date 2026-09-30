"""
Several connectors as one feed: equities from one provider and fixed
income from another, say. The rest of the app talks to it like any
other BaseMarketConnector.

  * symbols: every child's, in order (read live, so a child that adds
    symbols -- a newly issued T-bill -- is picked up);
  * stream(): the children's streams merged as ticks arrive. If one
    fails, the merged stream fails with its error;
  * fetch_history(): asked of whichever child carries the symbol;
  * running only while every child is; last_heartbeat is the oldest
    child's, so a stalled child makes the whole feed look stale.
"""

import asyncio
from datetime import datetime
from typing import AsyncIterator, Optional, Sequence

from app.connectors.base_connector import BaseMarketConnector
from app.models.candle import Candle
from app.models.tick import Tick

_DONE = object()


class _Failed:
    def __init__(self, error: BaseException):
        self.error = error


class CompositeConnector(BaseMarketConnector):

    def __init__(self, connectors: Sequence[BaseMarketConnector], queue_size: int = 1000):
        # Not BaseMarketConnector.__init__: symbols, running and
        # last_heartbeat are all read from the children.
        if not connectors:
            raise ValueError("CompositeConnector needs at least one connector")
        self.connectors = list(connectors)
        self._queue_size = queue_size

    @property
    def symbols(self) -> list[str]:
        return [s for c in self.connectors for s in c.symbols]

    @property
    def running(self) -> bool:
        return all(c.running for c in self.connectors)

    @property
    def last_heartbeat(self) -> Optional[datetime]:
        beats = [c.last_heartbeat for c in self.connectors]
        return None if any(b is None for b in beats) else min(beats)

    def owner(self, symbol: str) -> Optional[BaseMarketConnector]:
        return next((c for c in self.connectors if symbol in c.symbols), None)

    async def connect(self) -> None:
        for c in self.connectors:
            await c.connect()

    async def disconnect(self) -> None:
        for c in self.connectors:
            await c.disconnect()

    async def fetch_history(
        self, symbol: str, interval: str, start: Optional[datetime] = None
    ) -> list[Candle]:
        owner = self.owner(symbol)
        return [] if owner is None else await owner.fetch_history(symbol, interval, start)

    async def stream(self) -> AsyncIterator[Tick]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)

        async def pump(connector: BaseMarketConnector) -> None:
            try:
                async for tick in connector.stream():
                    await queue.put(tick)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                await queue.put(_Failed(e))
            else:
                await queue.put(_DONE)

        tasks = [asyncio.create_task(pump(c)) for c in self.connectors]
        try:
            finished = 0
            while finished < len(tasks):
                item = await queue.get()
                if item is _DONE:
                    finished += 1
                elif isinstance(item, _Failed):
                    raise item.error
                else:
                    yield item
                    # Let consumers run between ticks. A child can hand over
                    # a burst (every security's snapshot at startup, ~200
                    # ticks) that would otherwise pass through the buffer
                    # before anyone reads, overflowing its drop-oldest queues.
                    await asyncio.sleep(0)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
