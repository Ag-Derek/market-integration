"""
Merges every source into one list of calendar events for a date range.
Pure: it takes what the trackers and seed files hold and today's date,
so it can be tested on hand-made data.

**MPC decisions.** Past ones come from BoG's policy rate history (the
rate set, the rate before, the move); coming ones from the meeting dates
in data/economic_calendar.json, with the rate in force as the prior.
A meeting whose announcement day has a decision in the history is that
decision, with the meeting's dates added.

**GSS releases** keep GSS's week-of-month window (precision "week"), and
"done" means GSS marks them released. One still marked upcoming after
its window has passed is "overdue": GSS's status lags, or the release
slipped; either way the page says so rather than repeat "upcoming".

**T-bill auctions** are one event per tender, on the auction day (the
business day on or before the Friday before issue), with each security's
yield and its move. The next tender is the auction tracker's estimate.

**Holidays** are the market calendar's: GSE and GFIM closed.
"""

from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

from app.auctions import AuctionResult
from app.auctions.monitor import next_auction
from app.bond_math.conventions import BusinessCalendar
from app.econ import GssRelease, MpcMeeting, PolicyDecision
from app.models.econ import EconEvent, EventFigure, PolicyRateNow

BOG = "Bank of Ghana"
GSS = "Ghana Statistical Service"
MPC_URL = "https://www.bog.gov.gh/monetary-policy/mpc-meeting-dates/"
POLICY_URL = "https://www.bog.gov.gh/monetary-policy/policy-rate/"
GSS_URL = "https://statsghana.gov.gh/news-and-events/release-calendar"
AUCTION_URL = "https://www.bog.gov.gh/treasury-and-the-markets/treasury-bill-rates/"

# GSS category slug -> ours.
_GSS_CATEGORY = {
    "prices": "prices", "property": "prices",
    "economic-growth": "growth", "national-accounts": "growth", "business": "growth",
    "trade": "trade", "labour": "labour",
}
_HIGH = {"CPI", "QGDP", "AGDP"}
_MEDIUM = {"MIEG", "PPI", "QGDE", "IIP", "AGDE"}
# An announcement within this of a meeting's is the same decision (BoG
# has dated a few a day off; see app/econ/bog.py).
_MATCH_DAYS = 1


def policy_rate_now(decisions: Sequence[PolicyDecision], today: date) -> Optional[PolicyRateNow]:
    """The rate in force on `today`, when it was last set, and when and
    by how much it last moved."""
    held = [d for d in decisions if d.date <= today]
    if not held:
        return None
    latest = held[-1]
    changed, before = latest, None
    for prev in reversed(held[:-1]):
        if prev.rate != latest.rate:
            before = prev
            break
        changed = prev
    return PolicyRateNow(rate=latest.rate, since=latest.date, changed_on=changed.date,
                         change_bp=_bp(latest.rate - before.rate) if before else 0.0)


def mpc_events(decisions: Sequence[PolicyDecision], meetings: Sequence[MpcMeeting], today: date) -> list[EconEvent]:
    events, matched = [], set()
    for i, d in enumerate(decisions):
        prior = decisions[i - 1].rate if i else None
        meeting = next((m for m in meetings if abs((m.announcement - d.date).days) <= _MATCH_DAYS), None)
        if meeting:
            matched.add(meeting.announcement)
        events.append(_mpc_event(d.date, "done", d.rate, prior, meeting))
    for m in meetings:
        if m.announcement in matched:
            continue
        held = [d for d in decisions if d.date < m.announcement]
        prior = held[-1].rate if held else None
        status = "upcoming" if m.announcement > today else "due" if m.announcement == today else "overdue"
        if m.start <= today < m.announcement:
            status = "due"   # the committee is sitting
        events.append(_mpc_event(m.announcement, status, None, prior, m))
    return events


def _mpc_event(day: date, status: str, rate: Optional[float], prior: Optional[float],
               meeting: Optional[MpcMeeting]) -> EconEvent:
    note = None
    if meeting:
        span = meeting.start.strftime("%d %b").lstrip("0")
        if meeting.end != meeting.start:
            span += "–" + meeting.end.strftime("%d %b").lstrip("0")
        note = f"MPC meeting {span}; decision announced at the press release"
    return EconEvent(
        id=f"mpc-{day.isoformat()}", date=day, title="BoG policy rate decision", short_code="MPC",
        category="monetary_policy", importance="high", status=status, source=BOG,
        source_url=POLICY_URL if rate is not None else MPC_URL,
        figures=[EventFigure(label="Policy rate", value=rate, prior=prior,
                             change_bp=None if rate is None or prior is None else _bp(rate - prior))],
        note=note,
    )


def gss_events(releases: Iterable[GssRelease], today: date) -> list[EconEvent]:
    out = []
    for r in releases:
        first, last = r.window
        if r.status.lower() == "released":
            status = "done"
        elif today < first:
            status = "upcoming"
        elif today <= last:
            status = "due"
        else:
            status = "overdue"
        code = r.short_code
        out.append(EconEvent(
            id=f"gss-{r.id}", date=first, date_end=last, precision="month" if r.week is None else "week",
            title=r.title, short_code=code, category=_GSS_CATEGORY.get(r.category, "statistics"),
            importance="high" if code in _HIGH else "medium" if code in _MEDIUM else "low",
            status=status, source=GSS, source_url=GSS_URL, reference_period=r.reference_period,
            note=r.source_note if r.needs_verification else None,
        ))
    return out


def auction_events(results: Sequence[AuctionResult], previous, today: date,
                   business_days: BusinessCalendar) -> list[EconEvent]:
    """One event per tender in `results`, plus the next (estimated) one
    after the newest. `previous(result)` gives the same security's
    previous auction, as AuctionTracker.previous does."""
    by_issue: dict[date, list[AuctionResult]] = {}
    for r in results:
        by_issue.setdefault(r.issue_date, []).append(r)
    events = []
    for issue, rs in by_issue.items():
        rs.sort(key=lambda r: (r.kind != "bill", r.tenor_days or 0, r.security))
        figures = []
        for r in rs:
            p = previous(r)
            figures.append(EventFigure(
                label=_security_label(r), value=r.interest_rate, prior=p.interest_rate if p else None,
                change_bp=_bp(r.interest_rate - p.interest_rate) if p else None,
            ))
        bills_only = all(r.kind == "bill" for r in rs)
        events.append(EconEvent(
            id=f"auction-{issue.isoformat()}", date=_auction_day(issue, business_days),
            title=f"T-bill auction, tender {rs[0].tender}" if bills_only else f"GoG auction, tender {rs[0].tender}",
            short_code="TBILL", category="auction", importance="medium", status="done",
            source=BOG, source_url=AUCTION_URL, figures=figures,
            note=f"Issued {issue.strftime('%a %d %b').replace(' 0', ' ')}", link="/auctions",
        ))
    bills = [r for r in results if r.kind == "bill"]
    if bills:
        newest = max(r.issue_date for r in bills)
        tender = max(r.tender for r in bills if r.issue_date == newest)
        n_tender, auction, issue, state = next_auction(newest, tender, today, business_days)
        status = {"scheduled": "upcoming", "today": "due", "results_pending": "overdue"}[state]
        events.append(EconEvent(
            id=f"auction-{issue.isoformat()}", date=auction, title=f"T-bill auction, tender {n_tender}",
            short_code="TBILL", category="auction", importance="medium", status=status, estimated=True,
            source=BOG, source_url=AUCTION_URL,
            figures=[EventFigure(label=_security_label(r), prior=r.interest_rate)
                     for r in sorted((r for r in bills if r.issue_date == newest), key=lambda r: r.tenor_days)],
            note=f"Expected issue {issue.strftime('%a %d %b').replace(' 0', ' ')}; date estimated from the weekly pattern",
            link="/auctions",
        ))
    return events


def holiday_events(holidays: dict[date, str], today: date) -> list[EconEvent]:
    return [
        EconEvent(
            id=f"holiday-{day.isoformat()}", date=day, title=f"Market holiday: {name}", category="holiday",
            importance="medium", status="done" if day < today else "due" if day == today else "upcoming",
            source="GSE market calendar", note="GSE and GFIM closed",
        )
        for day, name in holidays.items()
    ]


_ORDER = {"high": 0, "medium": 1, "low": 2}


def calendar_events(
    start: date,
    end: date,
    today: date,
    decisions: Sequence[PolicyDecision],
    meetings: Sequence[MpcMeeting],
    releases: Iterable[GssRelease],
    auctions: Sequence[AuctionResult],
    previous_auction,
    holidays: dict[date, str],
    business_days: BusinessCalendar,
) -> list[EconEvent]:
    """Every event touching [start, end], by date, then importance."""
    # Auctions: only the tenders whose auction day can fall in range.
    near = [r for r in auctions if start - timedelta(days=7) <= r.issue_date <= end + timedelta(days=7)]
    if auctions:
        newest = max(r.issue_date for r in auctions if r.kind == "bill") if any(r.kind == "bill" for r in auctions) else None
        near += [r for r in auctions if r.issue_date == newest and r not in near]
    events = (
        mpc_events(decisions, meetings, today)
        + gss_events(releases, today)
        + auction_events(near, previous_auction, today, business_days)
        + holiday_events(holidays, today)
    )
    seen, out = set(), []
    for e in events:
        if e.id in seen or (e.date_end or e.date) < start or e.date > end:
            continue
        seen.add(e.id)
        out.append(e)
    return sorted(out, key=lambda e: (e.date, _ORDER[e.importance], e.title))


def _auction_day(issue: date, business_days: BusinessCalendar) -> date:
    day = issue - timedelta(days=3)
    while not business_days.is_trading_day(day):
        day -= timedelta(days=1)
    return day


def _security_label(r: AuctionResult) -> str:
    if r.kind == "bill":
        return f"{r.tenor_days}-day"
    years = r.security.split()[0]
    return f"{years}-year {r.kind}"


def _bp(pct: float) -> float:
    return round(pct * 100, 1)
