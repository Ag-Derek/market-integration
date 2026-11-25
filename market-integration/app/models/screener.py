"""
The stock screener's response (#99; Bloomberg's EQS): every GSE company
with a file in data/companies/, its valuation figures, the filters that
narrowed it and the companies each filter left out for lack of the
figure. See docs/screener.md and app/screener/.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ScreenerSort(str, Enum):
    """What the rows can be ordered by. Each has a fixed direction, the
    one a screen usually wants: highest yield, ROE and market cap first,
    cheapest P/E and P/B first. DCF upside joins once the DCF panel
    exists."""
    dividend_yield = "dividend_yield"
    pe_ratio = "pe_ratio"
    price_to_book = "price_to_book"
    return_on_equity = "return_on_equity"
    market_cap = "market_cap"
    symbol = "symbol"


class ScreenerFigure(BaseModel):
    """One calculated figure, as app/company/figures.py makes it: null
    with a `reason` when it can't be calculated."""
    value: Optional[float] = None
    formula: str
    reason: Optional[str] = None


class ScreenerColumn(BaseModel):
    """A figure the page shows as a column, with its formula for the
    header's hover and its unit."""
    key: str
    label: str
    formula: str
    unit: str


class ScreenerRow(BaseModel):
    symbol: str
    name: str
    sector: str
    price: ScreenerFigure
    market_cap: ScreenerFigure
    pe_ratio: ScreenerFigure
    price_to_book: ScreenerFigure
    dividend_yield: ScreenerFigure
    return_on_equity: ScreenerFigure


class ScreenerFilter(BaseModel):
    """A figure filter that was set, and the companies (in the sector, if
    one is set) it left out because they don't have the figure. They are
    never treated as zero. Counted per filter, so a company missing two
    filtered figures counts under each."""
    param: str
    figure: str
    label: str
    bound: str  # "min" or "max"
    threshold: float
    left_out: int
    reasons: dict[str, int]
    symbols: list[str]
    note: Optional[str] = None


class ScreenerResult(BaseModel):
    currency: str
    sector: Optional[str] = None
    sort: ScreenerSort
    universe: int  # companies screened: every company file, in the sector if one is set
    count: int  # rows that passed every filter
    filters: list[ScreenerFilter]
    columns: list[ScreenerColumn]
    sectors: list[str]  # every sector with a company file, for the page's Sector menu
    rows: list[ScreenerRow]
