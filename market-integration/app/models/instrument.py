"""
Static reference data for one tradable security -- what it *is*, not
what it trades at. The universe itself is seeded from
data/instruments.json (see app/instruments/).
"""

import re
from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, field_validator

AssetClass = Literal["equity", "bill", "bond"]
InstrumentStatus = Literal["active", "suspended", "delisted"]
# Finer-grained than asset_class for equities, so an ETF or preference
# share can still be told apart from an ordinary share.
EquityKind = Literal["ordinary", "preference", "depositary", "etf"]
# The sections of the GFIM daily trading report a fixed-income security
# is listed under (sell/buy-back trades are a trade type across the GoG
# segments, not a segment of their own). See docs/data-formats.md.
FixedIncomeSegment = Literal["new_gog", "ddep", "old_gog", "corporate", "treasury_bill"]

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

    # Fixed income only (None for equities). For bills and bonds the
    # symbol is the ISIN and the name is the GFIM security description,
    # e.g. "GOG-BD-17/08/27-A6139-1838-10.00", which has slashes.
    issuer: Optional[str] = None
    segment: Optional[FixedIncomeSegment] = None
    tenor: Optional[str] = None           # as the report labels it, e.g. "7-YEAR BOND", "2023-GC-3"
    maturity_date: Optional[date] = None
    coupon_rate: Optional[float] = None   # % a year; 0 for bills, None if the description has none

    @field_validator("symbol")
    @classmethod
    def _symbol_is_upper(cls, v: str) -> str:
        if not v or v != v.strip().upper():
            raise ValueError(f"symbol must be non-empty and upper-case, got {v!r}")
        return v

    @field_validator("isin")
    @classmethod
    def _isin_format_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not isin_format_ok(v):
            raise ValueError(f"invalid ISIN {v!r} (expected 2 letters, 9 alphanumerics, 1 digit)")
        return v
