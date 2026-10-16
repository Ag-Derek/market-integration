"""
The new-issue monitor behind /auctions/monitor (Bloomberg's NIM, for
the GoG primary market): the latest auction's rates and how they moved,
how recent new bills are trading in GFIM against their auction yield,
and what's next.

Pure functions over the tracker and the GFIM report, so they can be
tested on hand-made data.

**Matching an auction to its GFIM bill.** A bill auctioned for issue on
day D with tenor N days matures on D + N, and the GFIM report gives each
bill's tenor and maturity, so (tenor, maturity) finds it. That works for
the mock's bills too, which carry no BoG tender number.

**The next auction** is estimated from BoG's weekly pattern: tenders are
auctioned on Friday and issued the Monday after. A Monday holiday moves
the issue to the next business day and a Friday holiday the auction to
the business day before. BoG publishes a quarterly issuance calendar as
a PDF; we don't read it, so this is labelled an estimate.
"""

from datetime import date, timedelta
from typing import Optional, Sequence

from app.auctions import BILL_TENORS, AuctionResult
from app.auctions.tracker import AuctionTracker
from app.bond_math.conventions import BusinessCalendar
from app.models.auction import (
    AuctionMonitor,
    AuctionResultOut,
    GfimQuote,
    MaturingBill,
    NewIssue,
    NextAuction,
)
from app.models.fixed_income import TreasuryBillQuote

# How many recent tenders the new-issue table covers.
RECENT_TENDERS = 4


def result_out(r: AuctionResult, previous: Optional[AuctionResult]) -> AuctionResultOut:
    return AuctionResultOut(
        issue_date=r.issue_date, tender=r.tender, security=r.security, kind=r.kind,
        tenor_days=r.tenor_days, maturity_date=r.maturity_date,
        discount_rate=r.discount_rate, interest_rate=r.interest_rate,
        previous_issue_date=previous.issue_date if previous else None,
        previous_interest_rate=previous.interest_rate if previous else None,
        change_bp=_bp(r.interest_rate - previous.interest_rate) if previous else None,
    )


def gfim_bill(r: AuctionResult, bills: Sequence[TreasuryBillQuote]) -> Optional[TreasuryBillQuote]:
    """The GFIM bill an auction result created: same tenor, same maturity."""
    if r.kind != "bill":
        return None
    return next((b for b in bills if b.tenor == r.gfim_tenor and b.maturity_date == r.maturity_date), None)


def next_auction(latest_issue: date, latest_tender: int, today: date,
                 business_days: BusinessCalendar) -> tuple[int, date, date, str]:
    """(tender, auction date, issue date, status) for the next tender
    after the one issued on `latest_issue`. If BoG has gone quiet for
    more than a week past an expected auction, steps on a week at a time
    rather than show an auction long gone."""
    nominal_issue = latest_issue + timedelta(days=7)
    tender = latest_tender + 1
    while True:
        issue = _on_or_after(nominal_issue, business_days)
        auction = _on_or_before(nominal_issue - timedelta(days=3), business_days)
        if auction + timedelta(days=7) >= today:
            break
        nominal_issue += timedelta(days=7)
        tender += 1
    status = "scheduled" if auction > today else "today" if auction == today else "results_pending"
    return tender, auction, issue, status


def monitor(
    tracker: AuctionTracker,
    bills: Sequence[TreasuryBillQuote],
    gfim_report_date: Optional[date],
    today: date,
    business_days: BusinessCalendar,
    source: str,
    source_url: str,
) -> AuctionMonitor:
    latest = [r for r in (tracker.latest(s) for s in BILL_TENORS) if r is not None]
    newest_issue = max((r.issue_date for r in latest), default=None)
    latest_tender = [r for r in latest if r.issue_date == newest_issue]

    # The bill tenders of the last RECENT_TENDERS issue dates, newest first.
    recent_dates = sorted({r.issue_date for s in BILL_TENORS for r in tracker.history(s)}, reverse=True)
    recent_dates = recent_dates[:RECENT_TENDERS]
    new_issues = []
    if recent_dates:
        for r in tracker.results(since=recent_dates[-1]):
            if r.kind != "bill":
                continue
            b = gfim_bill(r, bills)
            new_issues.append(NewIssue(
                auction=result_out(r, tracker.previous(r)),
                gfim=None if b is None else GfimQuote(
                    symbol=b.symbol, description=b.description, closing_yield=b.closing_yield,
                    closing_price=b.closing_price, volume=b.volume, trade_count=b.trade_count,
                ),
                secondary_spread_bp=None if b is None or b.closing_yield is None
                else _bp(b.closing_yield - r.interest_rate),
            ))
    new_issues.sort(key=lambda n: (-n.auction.issue_date.toordinal(), n.auction.tenor_days or 0))

    upcoming = None
    if latest_tender:
        tender, auction, issue, status = next_auction(
            newest_issue, max(r.tender for r in latest_tender), today, business_days)
        upcoming = NextAuction(
            tender=tender, expected_auction_date=auction, expected_issue_date=issue, status=status,
            maturing=[
                MaturingBill(symbol=b.symbol, description=b.description, tenor=b.tenor,
                             maturity_date=b.maturity_date, closing_yield=b.closing_yield)
                for b in bills if b.maturity_date == issue
            ],
        )

    bonds = [r for r in (tracker.latest(s) for s in tracker.securities()) if r is not None and r.kind != "bill"]
    last_bond = max(bonds, key=lambda r: r.issue_date, default=None)

    return AuctionMonitor(
        as_of=tracker.as_of, source=source, source_url=source_url, status=tracker.describe(),
        gfim_report_date=gfim_report_date,
        latest=[result_out(r, tracker.previous(r)) for r in sorted(latest_tender, key=lambda r: r.tenor_days)],
        new_issues=new_issues,
        next_auction=upcoming,
        last_bond_auction=result_out(last_bond, tracker.previous(last_bond)) if last_bond else None,
    )


def _on_or_after(day: date, business_days: BusinessCalendar) -> date:
    while not business_days.is_trading_day(day):
        day += timedelta(days=1)
    return day


def _on_or_before(day: date, business_days: BusinessCalendar) -> date:
    while not business_days.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def _bp(pct: float) -> float:
    return round(pct * 100, 1)
