"""
Canonical market data shape used everywhere downstream of a connector.

Every connector (mock or real) is responsible for normalizing whatever
format its provider uses into this shape. Nothing outside of connectors/
should ever need to know a provider's raw field names.

Beyond the core tick (symbol, price, volume, timestamp), this also carries
the fundamentals a quote page needs (previous close, day/52-week range,
market cap, bid/ask, dividend info, ...). None of that exists in a real
GSE entitlement yet, so the mock connector fills it with generated
placeholder values -- see app/connectors/market_connector.py.
"""

from datetime import datetime

from pydantic import BaseModel, Field


class MarketData(BaseModel):
    symbol: str
    name: str
    exchange_label: str

    price: float = Field(gt=0)
    previous_close: float = Field(gt=0)
    open: float = Field(gt=0)
    day_high: float = Field(gt=0)
    day_low: float = Field(gt=0)
    week52_high: float = Field(gt=0)
    week52_low: float = Field(gt=0)

    market_cap: float = Field(gt=0)
    beta: float
    pe_ratio: float = Field(gt=0)
    eps: float

    # Either side of the book can be empty -- thinly traded GSE names
    # often close with only a bid, only an offer, or neither. An empty
    # side is None with a size of 0.
    bid: float | None = Field(default=None, gt=0)
    bid_size: int = Field(default=0, ge=0)
    ask: float | None = Field(default=None, gt=0)
    ask_size: int = Field(default=0, ge=0)

    volume: int = Field(ge=0)
    avg_volume: int = Field(ge=0)

    forward_dividend: float = Field(ge=0)
    forward_dividend_yield: float = Field(ge=0)
    ex_dividend_date: str
    earnings_date: str
    target_est: float = Field(gt=0)
    dividend_announcement: str | None = None

    timestamp: datetime
