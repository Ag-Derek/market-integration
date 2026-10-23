"""
Commodity reference prices: monthly averages of the world benchmarks
that matter to Ghana's economy (crude oil, gold, cocoa, ...), in US
dollars as published and in cedis at the Bank of Ghana's USD fixes.

These are reference prices, not tradable instruments: like the FX fixes
(app/fx/), they are pulled from a publisher, kept in SQLite exactly as
published, and turned into lookups on the way out. worldbank.py pulls
the World Bank's monthly "Pink Sheet", store.py keeps every series it
publishes (about 70), and prices.py offers the ones in COMMODITIES.

Prices are keyed by source, so a second publisher can sit beside the
World Bank: the Bank of Ghana's Monthly Commodity Prices (Brent, gold,
cocoa, and pump prices in cedis) is the one planned. A commodity names
its series at each source, preferred first; see docs/commodity-prices.md.
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

from app.models.fx import FxRate


@dataclass(frozen=True)
class Price:
    """One month's average price of one series, as its source published it."""

    source: str     # SOURCES key, e.g. "worldbank"
    series: str     # the source's name for it, footnote marks dropped: "Crude oil, Brent"
    unit: str       # as published, without the brackets: "$/bbl"
    month: date     # the first day of the month it averages
    value: float    # US dollars per unit


@dataclass(frozen=True)
class Release:
    """What one pull from a source gives: its prices, and the date it
    says it updated them, if it says."""

    updated_on: Optional[date]
    prices: list[Price]


@dataclass(frozen=True)
class Source:
    name: str
    url: str        # where a person can read it


SOURCES: dict[str, Source] = {
    "worldbank": Source(
        "World Bank Commodity Price Data (The Pink Sheet)",
        "https://www.worldbank.org/en/research/commodity-markets",
    ),
}


@dataclass(frozen=True)
class Commodity:
    code: str
    name: str
    group: str
    per: str                 # what one price buys, for display: "barrel"
    unit: str                # the published unit we expect; a series published in another is not offered
    series: dict[str, str]   # source -> its series name, preferred source first


def _c(code: str, name: str, group: str, per: str, unit: str, worldbank: str) -> Commodity:
    return Commodity(code, name, group, per, unit, {"worldbank": worldbank})


# The benchmarks offered, in display order: Ghana's exports (oil, gold,
# cocoa, ...), the staples and fertiliser it imports, and the metals its
# mining sector trades against. Every Pink Sheet series is stored, so
# offering another is one line here.
COMMODITIES: dict[str, Commodity] = {c.code: c for c in [
    _c("BRENT", "Brent crude oil", "Energy", "barrel", "$/bbl", "Crude oil, Brent"),
    _c("WTI", "WTI crude oil", "Energy", "barrel", "$/bbl", "Crude oil, WTI"),
    _c("GOLD", "Gold", "Metals", "troy oz", "$/troy oz", "Gold"),
    _c("SILVER", "Silver", "Metals", "troy oz", "$/troy oz", "Silver"),
    _c("ALUMINIUM", "Aluminium", "Metals", "tonne", "$/mt", "Aluminum"),
    _c("COPPER", "Copper", "Metals", "tonne", "$/mt", "Copper"),
    _c("COCOA", "Cocoa", "Agriculture", "kg", "$/kg", "Cocoa"),
    _c("COFFEE", "Coffee, Robusta", "Agriculture", "kg", "$/kg", "Coffee, Robusta"),
    _c("PALM_OIL", "Palm oil", "Agriculture", "tonne", "$/mt", "Palm oil"),
    _c("RUBBER", "Rubber, TSR20", "Agriculture", "kg", "$/kg", "Rubber, TSR20"),
    _c("COTTON", "Cotton, A Index", "Agriculture", "kg", "$/kg", "Cotton, A Index"),
    _c("MAIZE", "Maize", "Agriculture", "tonne", "$/mt", "Maize"),
    _c("SOYBEANS", "Soybeans", "Agriculture", "tonne", "$/mt", "Soybeans"),
    _c("RICE", "Rice, Thai 5%", "Agriculture", "tonne", "$/mt", "Rice, Thai 5%"),
    _c("WHEAT", "Wheat, US HRW", "Agriculture", "tonne", "$/mt", "Wheat, US HRW"),
    _c("SUGAR", "Sugar, world", "Agriculture", "kg", "$/kg", "Sugar, world"),
    _c("UREA", "Urea", "Fertiliser", "tonne", "$/mt", "Urea"),
]}


def month_key(month: date) -> str:
    """"2026-09" for September 2026."""
    return f"{month.year:04d}-{month.month:02d}"


def parse_month(text: str) -> date:
    """"2026-09" -> 1 Sep 2026. Raises ValueError for anything else."""
    year, _, month = text.partition("-")
    if len(year) != 4 or len(month) != 2:
        raise ValueError(f"expected YYYY-MM, got {text!r}")
    return date(int(year), int(month), 1)


def monthly_means(fixes: Iterable[FxRate]) -> dict[date, tuple[float, int]]:
    """Month (its first day) -> (mean mid, how many fixes) of the fixes
    dated in it. A month's average price in cedis is its average in
    dollars times this for USD: both averages over the same month."""
    by_month: dict[date, list[float]] = {}
    for f in fixes:
        by_month.setdefault(f.date.replace(day=1), []).append(f.mid)
    return {m: (math.fsum(mids) / len(mids), len(mids)) for m, mids in by_month.items()}
