import asyncio
import json
from datetime import date

import pytest

from app.auctions import AuctionResult
from app.bog import BogError
from app.econ import GssRelease, MpcMeeting, PolicyDecision, load_mpc_meetings
from app.econ.bog import parse_policy_page
from app.econ.events import (
    auction_events,
    calendar_events,
    gss_events,
    holiday_events,
    mpc_events,
    policy_rate_now,
)
from app.econ.gss import GssError, parse_calendar_page
from app.econ.store import EconStore
from app.econ.tracker import EconTracker

D = date.fromisoformat


class _Weekdays:
    def __init__(self, *holidays):
        self.holidays = {D(h) for h in holidays}

    def is_trading_day(self, day):
        return day.weekday() < 5 and day not in self.holidays


def _release(id_="2026-10-cpi-w2", month=10, week=2, status="Upcoming", code="CPI", category="prices", **kw):
    return GssRelease(id=id_, year=2026, month=month, week=week, title=kw.pop("title", "Consumer Price Index (CPI)"),
                      short_code=code, frequency="monthly", category=category, reference_period=None,
                      status=status, needs_verification=kw.pop("needs_verification", False), **kw)


DECISIONS = [PolicyDecision(D("2026-01-28"), 15.5), PolicyDecision(D("2026-03-18"), 14.0),
             PolicyDecision(D("2026-05-20"), 14.0), PolicyDecision(D("2026-09-24"), 14.0)]
MEETINGS = [MpcMeeting(D("2026-09-23"), D("2026-09-24"), D("2026-09-24")),
            MpcMeeting(D("2026-11-16"), D("2026-11-18"), D("2026-11-18"))]


# ---------------------------------------------------------------- BoG policy rate page

_POLICY_PAGE = (
    '<script>var chart = {"series":[{"type":"","name":"BOG Policy Rate","color":"","label":"BOG Policy Rate",'
    '"orig_header":"BOG Policy Rate","data":[25.5,27.5,14],"yAxis":0}],'
    '"xAxis":{"categories":["21 Nov 2002","24 Jan 2003","24 Sep 2026"],"crosshair":false}};</script>'
)


def test_policy_page_is_read_from_the_charts_data():
    assert parse_policy_page(_POLICY_PAGE) == [
        PolicyDecision(D("2002-11-21"), 25.5), PolicyDecision(D("2003-01-24"), 27.5), PolicyDecision(D("2026-09-24"), 14.0),
    ]


@pytest.mark.parametrize("page", [
    "<html>maintenance</html>",
    _POLICY_PAGE.replace('"24 Sep 2026"', '"24 Sep 2026","18 Nov 2026"'),     # dates and rates don't pair up
    _POLICY_PAGE.replace("[25.5,27.5,14]", "[25.5,27.5,140]"),                 # not a rate
    _POLICY_PAGE.replace('"24 Sep 2026"', '"2026-09-24"'),
])
def test_policy_page_errors(page):
    with pytest.raises(BogError):
        parse_policy_page(page)


# ---------------------------------------------------------------- GSS calendar page

def _gss_page(items) -> str:
    """The calendar as GSS's page carries it: JSON inside JS strings,
    split across several __next_f pushes."""
    payload = '5:["$","div",null,{"releases":' + json.dumps(items, separators=(",", ":")) + "}]"
    half = len(payload) // 2
    return "".join(
        '<script>self.__next_f.push([1,' + json.dumps(part) + '])</script>' for part in (payload[:half], payload[half:])
    )


_ITEM = {"id": "2026-10-cpi-w2", "sequence": 90, "status": "Upcoming", "year": 2026, "month": 10, "monthName": "October",
         "week": 2, "title": "Consumer Price Index (CPI) & Inflation", "shortCode": "CPI", "frequency": "monthly",
         "category": "prices", "referencePeriod": None, "needsVerification": False}


def test_gss_page_is_read_from_its_payload():
    no_week = dict(_ITEM, id="2026-03-policy-brief", week=None, shortCode=None, needsVerification=True,
                   sourceNote="No week stated in the source PDF.")
    got = parse_calendar_page(_gss_page([_ITEM, no_week]))
    assert got[0] == _release(title="Consumer Price Index (CPI) & Inflation")
    assert (got[0].short_code, got[0].window) == ("CPI", (D("2026-10-08"), D("2026-10-14")))
    assert got[1].week is None and got[1].window == (D("2026-10-01"), D("2026-10-31"))
    assert got[1].source_note == "No week stated in the source PDF."


@pytest.mark.parametrize("page", [
    "<html>redesigned</html>",
    _gss_page([dict(_ITEM, week=7)]),
    _gss_page([dict(_ITEM, month=13)]),
])
def test_gss_page_errors(page):
    with pytest.raises(GssError):
        parse_calendar_page(page)


def test_the_last_week_runs_to_the_months_end():
    assert _release(month=10, week=5).window == (D("2026-10-29"), D("2026-10-31"))
    assert _release(month=10, week=1).window == (D("2026-10-01"), D("2026-10-07"))


# ---------------------------------------------------------------- MPC seed file

def test_the_checked_in_mpc_dates_load():
    meetings = load_mpc_meetings()
    assert [m.announcement for m in meetings][-1] == D("2026-11-18")
    assert all(m.start <= m.end and m.start <= m.announcement for m in meetings)


def test_a_bad_mpc_file_names_itself(tmp_path):
    path = tmp_path / "econ.json"
    path.write_text('{"mpc_meetings": [{"start": "2026-11-18", "end": "2026-11-16", "announcement": "2026-11-18"}]}')
    with pytest.raises(ValueError, match="econ.json"):
        load_mpc_meetings(path)
    path.write_text("{}")
    with pytest.raises(ValueError, match="econ.json"):
        load_mpc_meetings(path)


# ---------------------------------------------------------------- store and tracker

def test_gss_calendar_is_replaced_a_year_at_a_time(tmp_path):
    store = EconStore(tmp_path / "e.db")
    store.replace_releases([_release(), _release("2026-11-cpi-w1", month=11, week=1)])
    # GSS moves October's CPI to week 3: the week-2 entry must go.
    store.replace_releases([_release("2026-10-cpi-w3", week=3), _release("2026-11-cpi-w1", month=11, week=1)])
    assert [r.id for r in store.releases()] == ["2026-10-cpi-w3", "2026-11-cpi-w1"]


class _Fails:
    def fetch(self):
        raise OSError("statsghana.gov.gh unreachable")


class _Gives:
    def __init__(self, rows):
        self.rows = rows

    def fetch(self):
        return self.rows


def test_one_source_failing_doesnt_stop_the_other(tmp_path):
    t = EconTracker(EconStore(tmp_path / "e.db"), _Gives(DECISIONS), _Fails())
    assert not asyncio.run(t.refresh())
    assert t.decisions == DECISIONS and t.releases == []
    d = t.describe()
    assert d["policy_rate"]["last_error"] is None and d["policy_rate"]["latest"] == "2026-09-24"
    assert "unreachable" in d["gss"]["last_error"]


# ---------------------------------------------------------------- events

def test_policy_rate_now_knows_when_it_last_moved():
    p = policy_rate_now(DECISIONS, D("2026-10-16"))
    assert (p.rate, p.since, p.changed_on, p.change_bp) == (14.0, D("2026-09-24"), D("2026-03-18"), -150.0)
    assert policy_rate_now(DECISIONS, D("2026-02-01")).rate == 15.5
    assert policy_rate_now(DECISIONS, D("2025-01-01")) is None


def test_mpc_events_past_from_history_coming_from_meetings():
    events = {e.date: e for e in mpc_events(DECISIONS, MEETINGS, D("2026-10-16"))}
    sep = events[D("2026-09-24")]
    assert (sep.status, sep.figures[0].value, sep.figures[0].prior, sep.figures[0].change_bp) == ("done", 14.0, 14.0, 0.0)
    assert "23 Sep–24 Sep" in sep.note       # matched to its meeting
    mar = events[D("2026-03-18")]
    assert mar.figures[0].change_bp == -150.0 and mar.note is None
    nov = events[D("2026-11-18")]
    assert (nov.status, nov.figures[0].value, nov.figures[0].prior) == ("upcoming", None, 14.0)
    assert len(events) == 5


@pytest.mark.parametrize("today, status", [
    ("2026-11-13", "upcoming"), ("2026-11-16", "due"), ("2026-11-18", "due"), ("2026-11-19", "overdue"),
])
def test_a_meeting_is_due_while_it_sits_and_overdue_without_a_decision(today, status):
    nov = next(e for e in mpc_events(DECISIONS, MEETINGS, D(today)) if e.date == D("2026-11-18"))
    assert nov.status == status


@pytest.mark.parametrize("gss_status, today, status", [
    ("Upcoming", "2026-10-07", "upcoming"),
    ("Upcoming", "2026-10-08", "due"),
    ("Upcoming", "2026-10-14", "due"),
    ("Upcoming", "2026-10-15", "overdue"),
    ("Released", "2026-10-09", "done"),
])
def test_gss_status(gss_status, today, status):
    (e,) = gss_events([_release(status=gss_status)], D(today))
    assert (e.status, e.precision, e.date, e.date_end, e.importance, e.category) == (
        status, "week", D("2026-10-08"), D("2026-10-14"), "high", "prices")


def test_gss_categories_and_doubtful_entries():
    (mieg,) = gss_events([_release(code="MIEG", category="economic-growth")], D("2026-10-01"))
    (brief,) = gss_events([_release(code=None, category="policy", needs_verification=True,
                                    source_note="No week stated.")], D("2026-10-01"))
    assert (mieg.category, mieg.importance) == ("growth", "medium")
    assert (brief.category, brief.importance, brief.note) == ("statistics", "low", "No week stated.")


def test_auction_events():
    results = [AuctionResult(D("2026-10-12"), 2028, "91 DAY BILL", 4.55, 4.6042),
               AuctionResult(D("2026-10-12"), 2028, "364 DAY BILL", 8.88, 9.7523),
               AuctionResult(D("2026-10-05"), 2027, "91 DAY BILL", 4.58, 4.6413)]
    prev = {("91 DAY BILL", D("2026-10-12")): results[2]}
    events = auction_events(results, lambda r: prev.get((r.security, r.issue_date)), D("2026-10-14"), _Weekdays())
    by_id = {e.id: e for e in events}
    held = by_id["auction-2026-10-12"]
    assert (held.date, held.status, held.title) == (D("2026-10-09"), "done", "T-bill auction, tender 2028")
    assert [(f.label, f.value, f.change_bp) for f in held.figures] == [("91-day", 4.6042, -3.7), ("364-day", 9.7523, None)]
    nxt = by_id["auction-2026-10-19"]
    assert (nxt.date, nxt.status, nxt.estimated, nxt.title) == (D("2026-10-16"), "upcoming", True, "T-bill auction, tender 2029")
    assert [(f.label, f.value, f.prior) for f in nxt.figures] == [("91-day", None, 4.6042), ("364-day", None, 9.7523)]


def test_holidays():
    (e,) = holiday_events({D("2026-12-04"): "Farmers' Day"}, D("2026-10-16"))
    assert (e.title, e.status, e.category) == ("Market holiday: Farmers' Day", "upcoming", "holiday")


def test_calendar_events_are_in_range_and_ordered():
    releases = [_release(), _release("2026-11-mieg-w2", month=11, week=2, code="MIEG", category="economic-growth"),
                _release("2026-12-cpi-w1", month=12, week=1)]
    events = calendar_events(
        D("2026-10-10"), D("2026-11-30"), D("2026-10-16"), DECISIONS, MEETINGS, releases,
        [], lambda r: None, {D("2026-12-04"): "Farmers' Day"}, _Weekdays(),
    )
    # October's CPI window (8-14) overlaps the start, so it's in; December's isn't.
    assert [e.id for e in events] == ["gss-2026-10-cpi-w2", "gss-2026-11-mieg-w2", "mpc-2026-11-18"]


# ---------------------------------------------------------------- API and page

def test_economic_calendar_api(client):
    from app import main

    main.econ._store.upsert_decisions(DECISIONS)
    main.econ._store.replace_releases([_release()])
    main.econ.reload()

    body = client.get("/economic-calendar", params={"start": "2026-09-01", "end": "2026-12-31"}).json()
    assert body["policy_rate"]["rate"] == 14.0
    ids = [e["id"] for e in body["events"]]
    assert "mpc-2026-09-24" in ids and "mpc-2026-11-18" in ids and "gss-2026-10-cpi-w2" in ids
    assert "holiday-2026-12-04" in ids
    assert [e["date"] for e in body["events"]] == sorted(e["date"] for e in body["events"])

    only = client.get("/economic-calendar", params={"start": "2026-09-01", "end": "2026-12-31",
                                                    "category": "monetary_policy"}).json()["events"]
    assert {e["category"] for e in only} == {"monetary_policy"}
    high = client.get("/economic-calendar", params={"start": "2026-09-01", "end": "2026-12-31",
                                                    "importance": "high"}).json()["events"]
    assert {e["importance"] for e in high} == {"high"}

    assert client.get("/economic-calendar", params={"category": "sport"}).status_code == 400
    assert client.get("/economic-calendar", params={"start": "2026-12-01", "end": "2026-11-01"}).status_code == 400
    assert client.get("/economic-calendar", params={"start": "2020-01-01", "end": "2026-01-01"}).status_code == 400
    assert client.get("/economic-calendar", params={"importance": "urgent"}).status_code == 422

    rates = client.get("/economic-calendar/policy-rate").json()
    assert [(r["date"], r["change_bp"]) for r in rates][:2] == [("2026-01-28", None), ("2026-03-18", -150.0)]
    assert len(client.get("/economic-calendar/policy-rate", params={"since": "2026-05-01"}).json()) == 2
    assert "economic_calendar" in client.get("/health").json()


def test_calendar_page_is_served_and_linked(client):
    page = client.get("/calendar").text
    assert "/economic-calendar" in page and 'href="/calendar" aria-current="page"' in page
    for path in ("/fixed-income", "/yield-curve", "/auctions", "/graph", "/ticker"):
        assert 'href="/calendar"' in client.get(path).text
