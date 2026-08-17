"""
Consumes MarketData off a queue, validates/normalizes state, and keeps
track of the latest known value per symbol.

The queue is what decouples ingestion speed from processing/broadcast
speed. If the provider bursts (e.g. 50,000 updates/sec), the connector's
producer task keeps draining into the queue without blocking on
downstream work, and this consumer processes at whatever pace it can
sustain. In a real deployment you'd bound the queue size and decide on
a backpressure policy (drop oldest, drop newest, block) — see the
`maxsize` note in main.py.
"""

import asyncio
from typing import Callable, Optional

from app.models.market_data import MarketData


class MarketProcessor:

    def __init__(self):
        self.latest_data: dict[str, MarketData] = {}

    def get_latest(self, symbol: str) -> Optional[MarketData]:
        return self.latest_data.get(symbol)

    def get_all_latest(self) -> dict[str, MarketData]:
        return self.latest_data.copy()

    async def process(self, data: MarketData) -> MarketData:
        """Validate/normalize a single item and update latest state."""
        # Pydantic already validated types/constraints on construction.
        # Additional business validation (staleness checks, duplicate
        # detection, etc.) belongs here as the pipeline grows.
        self.latest_data[data.symbol] = data
        return data

    async def consume(
        self,
        queue: "asyncio.Queue[MarketData]",
        on_processed: Optional[Callable[[MarketData], "asyncio.Future"]] = None,
    ) -> None:
        """
        Long-running consumer loop: pulls items off the queue, processes
        them, and optionally hands the result to a callback (e.g. the
        WebSocket gateway's broadcast) without the processor needing to
        know anything about delivery.
        """
        while True:
            data = await queue.get()
            try:
                processed = await self.process(data)
                if on_processed is not None:
                    await on_processed(processed)
            finally:
                queue.task_done()
