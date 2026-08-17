"""
Mock market data connector.

Simulates a live provider so the rest of the pipeline (queue, processor,
gateway, Symphony delivery) can be built and tested before a real
provider is chosen. When a provider is picked, write a new class here
(e.g. RealMarketConnector) that implements BaseMarketConnector the same
way — nothing else in the app needs to change.
"""

import asyncio
import random
from datetime import datetime, timezone
from typing import AsyncIterator

from app.connectors.base_connector import BaseMarketConnector
from app.models.market_data import MarketData


class MockMarketConnector(BaseMarketConnector):

    def __init__(self, symbols: list[str], interval_seconds: float = 0.5):
        super().__init__(symbols)
        self.interval_seconds = interval_seconds
        self._prices: dict[str, float] = {}

    async def connect(self) -> None:
        print("Connecting to (mock) market data provider...")
        await asyncio.sleep(1)

        self._prices = {
            symbol: random.uniform(100, 500)
            for symbol in self.symbols
        }

        self.running = True
        print("Connected to (mock) market data provider.")

    async def stream(self) -> AsyncIterator[MarketData]:
        if not self.running:
            raise RuntimeError("Connector is not connected. Call connect() first.")

        while self.running:
            for symbol in self.symbols:
                self._prices[symbol] += random.uniform(-1, 1)
                self._prices[symbol] = max(self._prices[symbol], 0.01)

                raw = {
                    "symbol": symbol,
                    "price": round(self._prices[symbol], 2),
                    "volume": random.randint(100, 5000),
                    "timestamp": datetime.now(timezone.utc),
                }

                yield self.normalize(raw)

            await asyncio.sleep(self.interval_seconds)

    async def disconnect(self) -> None:
        self.running = False
        print("Disconnected from (mock) market data provider.")

    def normalize(self, raw_data: dict) -> MarketData:
        # The mock already produces canonical field names, so this is a
        # pass-through. A real connector's normalize() would map the
        # provider's actual field names (e.g. sym/px/qty/ts) here.
        return MarketData(**raw_data)
