"""
Government of Ghana primary auctions, from the Bank of Ghana's Treasury
Bill Rates table: one row per security per weekly tender, back to 2013.

Each row is the issue date (the Monday after the Friday auction), the
tender number, the security as BoG names it ("91 DAY BILL", "2 YR FXR
NOTE", ...), and two rates. For bills those are the discount rate and
the interest rate (the yield; GFIM quotes bills on it, so it's what a
GFIM close compares with). For notes and bonds BoG gives the coupon
twice. Amounts offered, tendered and accepted are only in BoG press
releases, not this table. See
docs/fixed-income-sources-and-conventions.md.

bog.py pulls the table, store.py keeps it in SQLite as published,
tracker.py keeps it current and indexed, monitor.py turns it and the
GFIM report into the new-issue monitor behind /auctions.
"""

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from app.bond_math.conventions import add_months

SecurityKind = Literal["bill", "note", "bond"]

# "91 DAY BILL", "2 YR FXR NOTE", "15 YR FXR BOND"
_SECURITY = re.compile(r"^(\d+) (DAY|YR)(?: FXR)? (BILL|NOTE|BOND)$")
BILL_TENORS = ("91 DAY BILL", "182 DAY BILL", "364 DAY BILL")


@dataclass(frozen=True)
class AuctionResult:
    issue_date: date
    tender: int
    security: str          # as BoG writes it, upper case: "91 DAY BILL"
    discount_rate: float   # % a year; for notes and bonds, the coupon
    interest_rate: float   # % a year: a bill's yield; a note or bond's coupon

    @property
    def kind(self) -> SecurityKind:
        return _SECURITY.match(self.security).group(3).lower()

    @property
    def tenor_days(self) -> Optional[int]:
        """Days to maturity for a bill; None for notes and bonds."""
        n, unit, _ = _SECURITY.match(self.security).groups()
        return int(n) if unit == "DAY" else None

    @property
    def maturity_date(self) -> date:
        n, unit, _ = _SECURITY.match(self.security).groups()
        if unit == "DAY":
            return self.issue_date + timedelta(days=int(n))
        return add_months(self.issue_date, 12 * int(n))

    @property
    def gfim_tenor(self) -> Optional[str]:
        """The tenor as the GFIM report writes it ("91-DAY BILL"), for
        bills; None otherwise."""
        return self.security.replace(" DAY ", "-DAY ") if self.kind == "bill" else None


class NoRate(ValueError):
    """A row with a zero rate: a tender with no rate published (cancelled
    or fully rejected). A handful between 2016 and 2018."""


def parse_row(row: list) -> AuctionResult:
    """A row as BoG's table sends it: ["12 Oct 2026", "2028", "91 DAY
    BILL", "4.5518", "4.6042"]. Raises NoRate for a row with a zero
    rate, and ValueError for anything else unreadable, including a
    security we don't recognise or a rate that isn't a number."""
    if len(row) != 5:
        raise ValueError(f"expected 5 columns, got {len(row)}: {row!r}")
    day = datetime.strptime(str(row[0]).strip(), "%d %b %Y").date()
    tender = int(str(row[1]).replace(",", "").strip())
    security = " ".join(str(row[2]).upper().split())
    if not _SECURITY.match(security):
        raise ValueError(f"unrecognised security {row[2]!r}: {row!r}")
    rates = []
    for value in row[3:]:
        n = float(str(value).replace(",", "").strip())
        if n == 0:
            raise NoRate(f"no rate published: {row!r}")
        if not math.isfinite(n) or n < 0:
            raise ValueError(f"rate must be a positive number, got {value!r}: {row!r}")
        rates.append(n)
    return AuctionResult(day, tender, security, *rates)
