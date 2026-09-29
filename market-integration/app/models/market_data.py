"""
Canonical market data shape used everywhere downstream of a connector.

Every connector (mock or real) is responsible for normalizing whatever
format its provider uses into this shape. Nothing outside of connectors/
should ever need to know a provider's raw field names.

The trading fields follow the GSE "Daily Shares & ETFs" report:

  price             -- the GSE closing price, which is a volume-weighted
                       average (VWAP) of the session's trades, not the
                       last trade. Intraday it is the running VWAP so far;
                       with no trades yet it is the previous close, as
                       the GSE carries the closing price forward.
  last_trade_price  -- the most recent trade, whenever it happened.
  previous_close    -- the previous session's closing VWAP.
  change            -- price - previous_close (VWAP vs previous VWAP),
                       the GSE's definition of price change.
  shares_traded / value_traded -- session totals so far (value in GHS).
  year_high / year_low -- the report's year range. Whether the GSE means
                       the calendar year or a trailing 52 weeks is not
                       yet confirmed (issue #12); until it is, the
                       trailing 52-week range is kept in week52_*.

Beyond that, this also carries the fundamentals a quote page needs
(market cap, dividend info, ...). None of that exists in a real GSE
entitlement yet, so the mock connector fills it with generated
placeholder values -- see app/connectors/market_connector.py.
"""

from datetime import datetime

from pydantic import BaseModel, Field, computed_field


class MarketData(BaseModel):
    symbol: str
    name: str
    exchange_label: str

    price: float = Field(gt=0)
    last_trade_price: float = Field(gt=0)
    previous_close: float = Field(gt=0)
    # None until the session's first trade: a no-trade day has no open
    # or range.
    open: float | None = Field(default=None, gt=0)
    day_high: float | None = Field(default=None, gt=0)
    day_low: float | None = Field(default=None, gt=0)
    year_high: float = Field(gt=0)
    year_low: float = Field(gt=0)
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

    shares_traded: int = Field(ge=0)
    value_traded: float = Field(ge=0)
    avg_volume: int = Field(ge=0)

    forward_dividend: float = Field(ge=0)
    forward_dividend_yield: float = Field(ge=0)
    ex_dividend_date: str
    earnings_date: str
    target_est: float = Field(gt=0)
    dividend_announcement: str | None = None

    timestamp: datetime

    @computed_field
    @property
    def change(self) -> float:
        return round(self.price - self.previous_close, 2)

    @computed_field
    @property
    def change_percent(self) -> float:
        return round((self.price - self.previous_close) / self.previous_close * 100, 2)
