"""
End-of-day fixed-income quotes in the shape of the GFIM daily trading
report: one row type per report section, plus the section totals.

Field meanings follow the report; docs/data-formats.md maps each report
column to these fields. Every quote field is Optional because the
report leaves cells blank: no trades means no volume, trade count or
day range, and some securities have no prices at all. None here is a
blank cell there -- never zero.

There are deliberately no cross-field checks (e.g. low <= close <=
high). Real GFIM data breaks them routinely: end-of-day closing yields
fall outside the traded range, and T-bill day ranges come reversed
(docs/data-formats.md, quirks 7 and 9).
"""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.instrument import FixedIncomeSegment

GovernmentSegment = Literal["new_gog", "ddep", "old_gog"]
ReportSection = Literal["new_gog", "ddep", "old_gog", "treasury_bill", "corporate", "sell_buy_back"]
REPORT_SECTIONS: tuple[ReportSection, ...] = (
    "new_gog", "ddep", "old_gog", "treasury_bill", "corporate", "sell_buy_back",
)


class _Row(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    symbol: str                  # the ISIN
    description: str             # e.g. "GOG-BD-17/08/27-A6139-1838-10.00"
    maturity_date: date
    days_to_maturity: int        # computed from the report date (quirk 8)
    volume: Optional[int] = None         # face value traded; None = no trades
    trade_count: Optional[int] = None    # "number traded"; None = no trades


class GovernmentBondQuote(_Row):
    """A row of the New GoG, DDEP or Old GoG sections. Yields are % a
    year; prices are per 100 face value."""
    segment: GovernmentSegment
    tenor: str
    currency: str
    opening_yield: Optional[float] = None
    closing_yield: Optional[float] = None
    closing_price: Optional[float] = None
    day_low_yield: Optional[float] = None
    day_high_yield: Optional[float] = None


class TreasuryBillQuote(_Row):
    """A row of the Treasury Bills section. The day range is in price."""
    tenor: str                   # "91-DAY BILL", "182-DAY BILL", "364-DAY BILL"
    opening_price: Optional[float] = None
    opening_yield: Optional[float] = None
    closing_price: Optional[float] = None
    closing_yield: Optional[float] = None
    day_low_price: Optional[float] = None
    day_high_price: Optional[float] = None


class CorporateBondQuote(_Row):
    """A row of the Corporate section: prices only, no yields."""
    issuer: str
    opening_price: Optional[float] = None
    closing_price: Optional[float] = None
    day_low_price: Optional[float] = None
    day_high_price: Optional[float] = None


class SellBuyBackQuote(_Row):
    """A row of the Sell/Buy Back Trades section: a trade type on New GoG
    and DDEP bonds, so these repeat securities from those sections."""
    segment: GovernmentSegment
    tenor: str
    yield_: Optional[float] = Field(default=None, alias="yield")
    weighted_average_price: Optional[float] = None


class LargestTrade(BaseModel):
    """The SUMMARY sheet's "largest volume traded" for a section: the row
    with the most volume, and its yield and closing price."""
    model_config = ConfigDict(populate_by_name=True)

    symbol: str
    description: str
    volume: int
    trade_count: int
    yield_: Optional[float] = Field(default=None, alias="yield")
    closing_price: Optional[float] = None


class SectionSummary(BaseModel):
    section: ReportSection
    volume: int                  # sum of the section's rows; 0 if nothing traded
    trade_count: int
    largest_trade: Optional[LargestTrade] = None


class FixedIncomeSummary(BaseModel):
    report_date: date
    sections: list[SectionSummary]
    total_volume: int
    total_trade_count: int


class FixedIncomeReport(BaseModel):
    """The whole report for one session. Values are "so far" until the
    session closes."""
    report_date: date
    as_of: datetime
    # Where the report came from, for display; the mock's says it is
    # simulated, as MarketData.exchange_label does for equities.
    exchange_label: Optional[str] = None
    summary: FixedIncomeSummary
    new_gog: list[GovernmentBondQuote]
    ddep: list[GovernmentBondQuote]
    old_gog: list[GovernmentBondQuote]
    treasury_bill: list[TreasuryBillQuote]
    corporate: list[CorporateBondQuote]
    sell_buy_back: list[SellBuyBackQuote]


# ---------------------------------------------------------------- yield curve


CurveSegment = Literal["treasury_bill", "new_gog", "ddep", "old_gog"]


class CurveInstrument(BaseModel):
    symbol: str
    description: str
    tenor: str                   # "91-DAY BILL", "2023-GC-3", ...


class CurvePoint(BaseModel):
    """One (tenor, yield) point of the curve. Bills of different tenors
    maturing the same day share a close, so they are one point with
    several instruments behind it; a bond is one point, one instrument."""
    model_config = ConfigDict(populate_by_name=True)

    tenor_years: float           # time to maturity from the session date, ACT/365.25
    days_to_maturity: int
    yield_: float = Field(alias="yield")   # closing yield, % a year
    closing_price: Optional[float] = None
    segment: CurveSegment
    maturity_date: date
    instruments: list[CurveInstrument]


class GovernmentYieldCurve(BaseModel):
    """The GHS Government of Ghana curve at a session's close (or so far,
    for the current session): bills, New GoG, 2023 DDEP and Old GoG
    bonds. GFSF bonds (structured) and USD DDE bonds (another currency)
    are left out."""
    date: date                   # the session: the latest on or before the date asked for
    as_of: datetime
    currency: str = "GHS"
    exchange_label: Optional[str] = None
    points: list[CurvePoint]     # by tenor


# ---------------------------------------------------------------- ticks
# The pipeline's shape for fixed income: one message per security, like
# the equity MarketData, and in the same discriminated union
# (app/models/tick.py). The report rows above are the GFIM report as
# published; these are what a connector normalizes a quote into.


class FixedIncomeTick(BaseModel):
    """A bill or bond quote. Prices are clean, per 100 face value; yields
    are % a year.

    Every price and yield is optional: government securities are quoted
    by yield with a price derived from it, corporates by price only, and
    a security that hasn't traded or been quoted has neither. Nothing is
    range-checked here -- the validator does that, so a bad value is
    logged and dropped instead of failing the connector -- and there are
    deliberately no low <= close <= high checks at all (see the module
    docstring)."""
    tick_type: Literal["fixed_income"] = "fixed_income"

    symbol: str                  # the ISIN
    name: str                    # the GFIM description
    segment: FixedIncomeSegment
    currency: str                # GHS, except the four USD DDE bonds
    maturity_date: date
    # Source and currency for display, as MarketData.exchange_label; the
    # mock's says "Simulated".
    exchange_label: Optional[str] = None

    # Two-way quote. Not in the end-of-day report; expected from the API.
    bid_price: Optional[float] = None
    ask_price: Optional[float] = None
    bid_yield: Optional[float] = None
    ask_yield: Optional[float] = None

    # The session so far, as in the report. The closing values are the
    # GFIM end-of-day methodology's, not the last trade's.
    opening_yield: Optional[float] = None
    closing_yield: Optional[float] = None
    day_low_yield: Optional[float] = None
    day_high_yield: Optional[float] = None
    opening_price: Optional[float] = None
    closing_price: Optional[float] = None
    day_low_price: Optional[float] = None
    day_high_price: Optional[float] = None
    volume: int = Field(default=0, ge=0)       # face value traded this session, cumulative
    trade_count: int = Field(default=0, ge=0)

    # Same two clocks as MarketData: when the feed published this, and
    # when the security last traded (None if unknown or never).
    timestamp: datetime
    last_trade_at: Optional[datetime] = None


class RepoTick(BaseModel):
    """A bond's sell/buy-back trades this session. These are repos --
    financing, not outright trades -- so their yields and prices must
    never reach price charts or yield curves: the aggregator records
    them in a table of their own, and the processor keeps them apart
    from the bond's quote."""
    tick_type: Literal["repo"] = "repo"

    symbol: str
    name: str
    segment: GovernmentSegment
    currency: str
    maturity_date: date
    # The bond that changed hands in the sell leg, from the report's
    # "Yield" and "Weighted average closing prices" columns: the
    # collateral's yield (% a year) and clean price per 100. Neither is
    # the financing rate. For the USD DDE bonds the report's yield column
    # holds the price (quirk 12); a connector must leave bond_yield None
    # then rather than map a price into it, and the validator rejects a
    # tick where the two are equal.
    bond_yield: Optional[float] = None
    bond_price: Optional[float] = None
    # The repo (financing) rate, % a year. Not in the GFIM report; None
    # until a source provides it.
    repo_rate: Optional[float] = None
    volume: int = Field(default=0, ge=0)
    trade_count: int = Field(default=0, ge=0)

    timestamp: datetime
    last_trade_at: Optional[datetime] = None
