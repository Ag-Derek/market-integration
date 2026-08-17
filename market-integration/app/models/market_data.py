"""
Canonical market data shape used everywhere downstream of a connector.

Every connector (mock or real) is responsible for normalizing whatever
format its provider uses into this shape. Nothing outside of connectors/
should ever need to know a provider's raw field names.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class MarketData(BaseModel):
    symbol: str
    price: float = Field(gt=0)
    volume: int = Field(ge=0)
    timestamp: datetime
