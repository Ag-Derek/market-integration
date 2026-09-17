"""
Consumes MarketData off a validated feed, tracks the latest known value
per symbol, and hands each tick to a delivery callback.

The feed comes from a subscription on the MarketDataBuffer (see
app.queue.market_buffer), which is what decouples ingestion speed from
processing/broadcast speed and applies backpressure (bounded
per-subscriber queue, drop-oldest) if this consumer falls behind.
"""

from typing import AsyncIterator, Callable, Optional

from app.models.market_data import MarketData


class MarketProcessor:

    def __init__(self):
        self.latest_data: dict[str, MarketData] = {}

    def get_latest(self, symbol: str) -> Optional[MarketData]:
        return self.latest_data.get(symbol)

    def get_all_latest(self) -> dict[str, MarketData]:
        return self.latest_data.copy()

    async def process(self, data: MarketData) -> MarketData:
        """Update latest state for a single, already-validated tick."""
        self.latest_data[data.symbol] = data
        return data

    async def consume(
        self,
        feed: AsyncIterator[MarketData],
        on_processed: Optional[Callable[[MarketData], None]] = None,
    ) -> None:
        """
        Long-running consumer loop: pulls items off `feed`, processes
        them, and optionally hands the result to a callback (e.g. the
        WebSocket gateway's broadcast) without the processor needing to
        know anything about delivery.
        """
        async for data in feed:
            processed = await self.process(data)
            if on_processed is not None:
                await on_processed(processed)
