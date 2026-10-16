"""
The auction tracker's API shapes (app/auctions/, /auctions/...). Rates
are % a year; changes and spreads are in basis points.
"""

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel


class AuctionResultOut(BaseModel):
    issue_date: date
    tender: int
    security: str                    # as BoG writes it: "91 DAY BILL", "2 YR FXR NOTE"
    kind: Literal["bill", "note", "bond"]
    tenor_days: Optional[int] = None  # bills only
    maturity_date: date
    discount_rate: float             # bills: discount rate; notes and bonds: the coupon
    interest_rate: float             # bills: yield; notes and bonds: the coupon
    # The same security's previous auction, and the move since (bp).
    previous_issue_date: Optional[date] = None
    previous_interest_rate: Optional[float] = None
    change_bp: Optional[float] = None


class GfimQuote(BaseModel):
    """The bill the auction created, as it stands in today's GFIM report."""
    symbol: str
    description: str
    closing_yield: Optional[float] = None
    closing_price: Optional[float] = None
    volume: Optional[int] = None
    trade_count: Optional[int] = None


class NewIssue(BaseModel):
    auction: AuctionResultOut
    gfim: Optional[GfimQuote] = None     # None until the bill shows up in GFIM, or once it has matured
    # GFIM close minus the auction yield, bp: positive means yields have
    # risen (prices fallen) since the auction.
    secondary_spread_bp: Optional[float] = None


class MaturingBill(BaseModel):
    symbol: str
    description: str
    tenor: str
    maturity_date: date
    closing_yield: Optional[float] = None


class NextAuction(BaseModel):
    """An estimate from BoG's weekly pattern (auction on Friday, issue on
    the Monday after, each moved back or forward round holidays on the
    GFIM calendar), not a published calendar."""
    tender: int                          # the latest tender + 1
    expected_auction_date: date
    expected_issue_date: date
    # "scheduled": the auction is ahead; "today"; "results_pending": its
    # date has passed but BoG hasn't published the rates yet.
    status: Literal["scheduled", "today", "results_pending"]
    # Bills in today's GFIM report maturing on the expected issue date:
    # what the new issue rolls over.
    maturing: list[MaturingBill]


class AuctionMonitor(BaseModel):
    as_of: Optional[date] = None         # the newest issue date held
    source: str
    source_url: str
    status: dict                         # the tracker's describe(): last pull, errors
    gfim_report_date: Optional[date] = None
    latest: list[AuctionResultOut]       # the latest tender's bills, by tenor
    new_issues: list[NewIssue]           # bills from the recent tenders, newest first
    next_auction: Optional[NextAuction] = None
    last_bond_auction: Optional[AuctionResultOut] = None  # the latest note or bond, however long ago
