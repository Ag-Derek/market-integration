"""
A Ghana Commodity Exchange (GCX) spot contract's quote, in the shape of
the GCX market report: one row per contract with its opening and
closing prices, the session's high and low, and the volume traded. See
docs/commodities-gcx.md for the report and how each column maps here.

Prices are in cedis per metric tonne, as GCX quotes them. Volumes are in
kilograms rather than the report's tonnes (it gives four decimals, i.e.
to the 100 g): candles keep volume as a whole number, so tonnes would
lose everything after the point. 1 MT = 1,000 kg.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.models.instrument import Commodity, CommodityUnit

KG_PER_MT = 1000
# GCX's own price posts quote per 50 kg bag as well as per tonne.
KG_PER_BAG = 50


def format_tonnes(quantity: float) -> str:
    """Metric tonnes as GCX writes them, to 4 decimals at most and without
    trailing zeros: "10.7826 MT", "1,500 MT"."""
    return f"{quantity:,.4f}".rstrip("0").rstrip(".") + " MT"


class CommodityTick(BaseModel):
    """A GCX contract's quote: the session so far, and the order book.

    `open`, `day_high` and `day_low` are None until the contract trades
    in the session, as the report leaves them blank ("-"). `price` is the
    last trade, which is the report's closing price once the session
    ends; with no trade yet this session it carries the previous
    close over, so `change` is 0."""
    tick_type: Literal["commodity"] = "commodity"

    symbol: str                  # GCX's code, e.g. "GEJWM2"
    name: str                    # "White Maize, Grade 2, Ejura"
    commodity: Commodity
    grade: int
    delivery_centre: str         # the GCX warehouse it is delivered to
    currency: str = "GHS"
    unit: CommodityUnit = "MT"   # prices are per this
    # Source and currency for display, as MarketData.exchange_label; the
    # mock's says "Simulated".
    exchange_label: Optional[str] = None

    price: float = Field(gt=0)            # last trade, GHS per MT
    previous_close: float = Field(gt=0)   # the last session's closing price
    change: float                         # price - previous_close
    open: Optional[float] = Field(default=None, gt=0)
    day_high: Optional[float] = Field(default=None, gt=0)
    day_low: Optional[float] = Field(default=None, gt=0)
    week52_high: float = Field(gt=0)
    week52_low: float = Field(gt=0)

    # Best bid and offer, GHS per MT, and the kilograms at each. Either
    # side can be empty, as on the GSE: None with a size of 0.
    bid: Optional[float] = Field(default=None, gt=0)
    bid_size: int = Field(default=0, ge=0)
    ask: Optional[float] = Field(default=None, gt=0)
    ask_size: int = Field(default=0, ge=0)

    volume: int = Field(ge=0)             # kg traded this session, cumulative
    value_traded: float = Field(ge=0)     # GHS turnover this session
    trade_count: int = Field(default=0, ge=0)
    avg_volume: int = Field(ge=0)         # typical kg traded a day

    # Same two clocks as MarketData: when the feed published this, and
    # when the contract last traded (None if unknown or never).
    timestamp: datetime
    last_trade_at: Optional[datetime] = None
