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
    summary: FixedIncomeSummary
    new_gog: list[GovernmentBondQuote]
    ddep: list[GovernmentBondQuote]
    old_gog: list[GovernmentBondQuote]
    treasury_bill: list[TreasuryBillQuote]
    corporate: list[CorporateBondQuote]
    sell_buy_back: list[SellBuyBackQuote]
