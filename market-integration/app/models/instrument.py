"""
Static reference data for one tradable security -- what it *is*, not
what it trades at. The universe itself is seeded from
data/instruments.json (see app/instruments/).
"""

import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

AssetClass = Literal["equity", "bill", "bond", "commodity"]
InstrumentStatus = Literal["active", "suspended", "delisted"]
# Finer-grained than asset_class for equities, so an ETF or preference
# share can still be told apart from an ordinary share.
EquityKind = Literal["ordinary", "preference", "depositary", "etf"]
# The GSE board an equity is listed on: the Main Market or the Ghana
# Alternative Market (GAX), as the GSE's listed-companies page groups them.
Board = Literal["main", "gax"]
# The sections of the GFIM daily trading report a fixed-income security
# is listed under (sell/buy-back trades are a trade type across the GoG
# segments, not a segment of their own). See docs/data-formats.md.
FixedIncomeSegment = Literal["new_gog", "ddep", "old_gog", "corporate", "treasury_bill"]
# Day-count bases in use on the GFIM; see
# docs/fixed-income-sources-and-conventions.md.
DayCount = Literal["ACT/364", "ACT/365", "ACT/ACT"]
# The crops a Ghana Commodity Exchange (GCX) spot contract can be for,
# with the code GCX symbols carry for each: GEJWM2 is White Maize ("WM")
# at Ejura, grade 2. See docs/commodities-gcx.md.
Commodity = Literal["white_maize", "yellow_maize", "soya_bean", "sesame", "sorghum", "rice"]
COMMODITY_NAMES: dict[str, str] = {
    "white_maize": "White Maize",
    "yellow_maize": "Yellow Maize",
    "soya_bean": "Soya Bean",
    "sesame": "Sesame",
    "sorghum": "Sorghum",
    "rice": "Rice",
}
# What a GCX price is per, and what volume is counted in.
CommodityUnit = Literal["MT"]
_COMMODITY_FIELDS = ("commodity", "grade", "delivery_centre", "unit")

_ISIN_FORMAT = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


def isin_format_ok(isin: str) -> bool:
    """Two-letter country code, nine alphanumerics, one digit.

    Deliberately no ISO 6166 check-digit test: ISINs come from the GSE,
    which is authoritative, and some older GSE-assigned codes don't
    satisfy the check digit (e.g. MAC's GH0000000118). This only catches
    a mangled value, such as one with a character missing or doubled."""
    return _ISIN_FORMAT.fullmatch(isin) is not None


class Instrument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    name: str
    asset_class: AssetClass
    sector: str
    currency: str = "GHS"
    isin: Optional[str] = None
    status: InstrumentStatus = "active"
    kind: Optional[EquityKind] = None
    board: Optional[Board] = None         # equities only; None where not known
    # Other listed lines of the same issuer, e.g. AGA's depositary shares
    # AADS, or SCB's preference shares SCB-PREF. Must be mutual: the seed
    # is rejected unless each side lists the other (app/instruments/).
    related_symbols: tuple[str, ...] = ()

    # Fixed income only (None for equities). For bills and bonds the
    # symbol is the ISIN and the name is the GFIM security description,
    # e.g. "GOG-BD-17/08/27-A6139-1838-10.00", which has slashes.
    issuer: Optional[str] = None
    segment: Optional[FixedIncomeSegment] = None
    tenor: Optional[str] = None           # as the report labels it, e.g. "7-YEAR BOND", "2023-GC-3"
    maturity_date: Optional[date] = None
    coupon_rate: Optional[float] = None   # % a year; 0 for bills, None if the description has none
    # The terms bond math needs. None means not known yet, not "none":
    # corporates, GFSF and USD DDE bonds need term sheets first
    # (docs/fixed-income-sources-and-conventions.md), and the report
    # gives issue dates for bills only (maturity minus tenor).
    issue_date: Optional[date] = None
    frequency: Optional[int] = Field(default=None, ge=0)  # coupons a year; 0 = zero coupon (bills)
    day_count: Optional[DayCount] = None
    face_value: Optional[float] = Field(default=None, gt=0)  # what prices are quoted per: 100

    # Commodities only (None for everything else): a GCX spot contract is
    # one crop, of one grade, delivered to one GCX warehouse, priced in
    # GHS per `unit`. The symbol is GCX's code, e.g. "GEJWM2".
    commodity: Optional[Commodity] = None
    grade: Optional[int] = Field(default=None, ge=1, le=5)
    delivery_centre: Optional[str] = None
    unit: Optional[CommodityUnit] = None

    @model_validator(mode="after")
    def _commodity_terms(self) -> "Instrument":
        given = [f for f in _COMMODITY_FIELDS if getattr(self, f) is not None]
        if self.asset_class == "commodity":
            missing = [f for f in _COMMODITY_FIELDS if f not in given]
            if missing:
                raise ValueError(f"a commodity needs {', '.join(missing)}")
        elif given:
            raise ValueError(f"{', '.join(given)} are for commodities, not a {self.asset_class}")
        return self

    @model_validator(mode="after")
    def _issued_before_maturity(self) -> "Instrument":
        if self.issue_date and self.maturity_date and self.issue_date >= self.maturity_date:
            raise ValueError(
                f"issue_date ({self.issue_date}) must be before maturity_date ({self.maturity_date})"
            )
        return self

    @field_validator("symbol")
    @classmethod
    def _symbol_is_upper(cls, v: str) -> str:
        if not v or v != v.strip().upper():
            raise ValueError(f"symbol must be non-empty and upper-case, got {v!r}")
        return v

    @model_validator(mode="after")
    def _related_are_other_symbols(self) -> "Instrument":
        if self.symbol in self.related_symbols:
            raise ValueError(f"{self.symbol} lists itself in related_symbols")
        if len(set(self.related_symbols)) != len(self.related_symbols):
            raise ValueError(f"{self.symbol} lists a related symbol twice")
        return self

    @field_validator("isin")
    @classmethod
    def _isin_format_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not isin_format_ok(v):
            raise ValueError(f"invalid ISIN {v!r} (expected 2 letters, 9 alphanumerics, 1 digit)")
        return v
