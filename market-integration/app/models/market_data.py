"""
Canonical market data shape used everywhere downstream of a connector.

Every connector (mock or real) is responsible for normalizing whatever
format its provider uses into this shape. Nothing outside of connectors/
should ever need to know a provider's raw field names.

The trading fields follow the GSE "Daily Shares & ETFs" report; see
docs/data-formats.md for the column-by-column mapping. Beyond those,
this also carries the fundamentals a quote page needs (market cap,
dividend info, ...). None of that exists in a real GSE entitlement yet,
so the mock connector fills it with generated placeholder values -- see
app/connectors/market_connector.py.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class MarketData(BaseModel):
    # Tells equity ticks from fixed-income ones in the pipeline's
    # discriminated union (app/models/tick.py).
    tick_type: Literal["equity"] = "equity"

    symbol: str
    name: str
    exchange_label: str

    # Field meanings follow the GSE daily shares report; see
    # docs/data-formats.md for the column-by-column mapping.
    price: float = Field(gt=0)            # last transaction price
    previous_close: float = Field(gt=0)   # previous session's VWAP, not its last trade
    open: float = Field(gt=0)
    # Session VWAP: the GSE's official closing price. Carries the previous
    # one over when there have been no trades this session.
    vwap: float = Field(gt=0)
    change: float                         # vwap - previous_close, not price - previous_close
    day_high: float = Field(gt=0)
    day_low: float = Field(gt=0)
    # The report's "Year High/Low". Whether the GSE means the calendar
    # year or a trailing 52 weeks is unconfirmed (docs/data-formats.md,
    # open questions); the trailing 52-week range is kept alongside
    # until it is.
    year_high: float = Field(gt=0)
    year_low: float = Field(gt=0)
    week52_high: float = Field(gt=0)
    week52_low: float = Field(gt=0)

    # Price x shares outstanding, set in the pipeline from the company
    # data (app/company/figures.py); None where shares outstanding
    # aren't recorded. Connectors leave it None.
    market_cap: Optional[float] = Field(default=None, gt=0)
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

    volume: int = Field(ge=0)             # shares traded this session
    value_traded: float = Field(ge=0)     # GHS turnover this session
    avg_volume: int = Field(ge=0)

    forward_dividend: float = Field(ge=0)
    forward_dividend_yield: float = Field(ge=0)
    ex_dividend_date: str
    earnings_date: str
    target_est: float = Field(gt=0)
    dividend_announcement: str | None = None

    # Two different clocks. `timestamp` is when the feed published this
    # quote, so it says how fresh the *feed* is. `last_trade_at` is when
    # the symbol last traded -- for a thin GSE name that can be hours or
    # days ago on a perfectly healthy feed. None if unknown or never.
    timestamp: datetime
    last_trade_at: datetime | None = None
