"""
Consumes ticks off a validated feed, tracks the latest known value per
symbol, and hands each tick to a delivery callback.

The feed comes from a subscription on the MarketDataBuffer (see
app.queue.market_buffer), which is what decouples ingestion speed from
processing/broadcast speed and applies backpressure (bounded
per-subscriber queue, drop-oldest) if this consumer falls behind.

A bond's repo (sell/buy-back) ticks share its symbol but are not quotes,
so they are kept apart: get_latest() is always the bond's quote, and
get_latest_repo() its repo activity.
"""

from typing import AsyncIterator, Callable, Optional

from app.models.fixed_income import RepoTick
from app.models.tick import Tick


class MarketProcessor:

    def __init__(self):
        self.latest_data: dict[str, Tick] = {}
        self.latest_repos: dict[str, RepoTick] = {}

    def get_latest(self, symbol: str) -> Optional[Tick]:
        return self.latest_data.get(symbol)

    def get_latest_repo(self, symbol: str) -> Optional[RepoTick]:
        return self.latest_repos.get(symbol)

    def get_all_latest(self) -> dict[str, Tick]:
        return self.latest_data.copy()

    async def process(self, data: Tick) -> Tick:
        """Update latest state for a single, already-validated tick."""
        if isinstance(data, RepoTick):
            self.latest_repos[data.symbol] = data
        else:
            self.latest_data[data.symbol] = data
        return data

    async def consume(
        self,
        feed: AsyncIterator[Tick],
        on_processed: Optional[Callable[[Tick], None]] = None,
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
