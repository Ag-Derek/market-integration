"""
Contract that every market data connector must satisfy.

Whichever provider you eventually pick (a specific vendor API, an
exchange's own feed, etc.), you write ONE class that implements this
interface. Nothing else in the app changes: main.py, the processor,
and the gateway only ever talk to BaseMarketConnector.

Lifecycle expected by callers:

    connector = SomeConnector(...)
    await connector.connect()          # auth + subscribe happens here
    async for data in connector.stream():
        ...                            # data is already a MarketData
    await connector.disconnect()

Implementations are responsible for:
- authentication
- subscribing to the requested symbols
- converting provider-specific messages into MarketData (normalize())
- deciding what "connected" / "running" means for reconnection logic
"""

from abc import ABC, abstractmethod
from typing import AsyncIterator

from app.models.market_data import MarketData


class BaseMarketConnector(ABC):

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self.running = False

    @abstractmethod
    async def connect(self) -> None:
        """Establish the connection (and authenticate/subscribe if needed)."""
        raise NotImplementedError

    @abstractmethod
    def stream(self) -> AsyncIterator[MarketData]:
        """
        Async generator yielding normalized MarketData as it arrives.
        Must only yield data after connect() has succeeded.
        """
        raise NotImplementedError

    @abstractmethod
    async def disconnect(self) -> None:
        """Tear down the connection cleanly."""
        raise NotImplementedError

    def normalize(self, raw_data: dict) -> MarketData:
        """
        Convert a provider's raw message into MarketData.
        Override this in real connectors; the default assumes the raw
        dict already matches MarketData's field names, which is only
        true for mocks/tests.
        """
        return MarketData(**raw_data)
