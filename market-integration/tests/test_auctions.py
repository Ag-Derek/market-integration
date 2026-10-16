import asyncio
import json
from datetime import date, datetime, timezone
from urllib.parse import parse_qs

import pytest

from app.auctions import AuctionResult, NoRate, parse_row
from app.auctions.bog import PAGE_URL, BogAuctionClient
from app.auctions.monitor import gfim_bill, monitor, next_auction
from app.auctions.store import AuctionStore
from app.auctions.tracker import REFETCH_DAYS, AuctionTracker
from app.models.fixed_income import TreasuryBillQuote


def _r(day, security="91 DAY BILL", rate=4.6, tender=2028, discount=None):
    return AuctionResult(date.fromisoformat(day), tender, security, discount or rate - 0.05, rate)


class _Weekdays:
    """Business days: Monday to Friday, less `holidays`."""
    def __init__(self, *holidays):
        self.holidays = {date.fromisoformat(h) for h in holidays}

    def is_trading_day(self, day):
        return day.weekday() < 5 and day not in self.holidays


# ---------------------------------------------------------------- parsing

def test_parse_row_reads_bogs_format():
    r = parse_row(["12 Oct 2026", "2028", "91 DAY BILL", "4.5518", "4.6042"])
    assert r == AuctionResult(date(2026, 10, 12), 2028, "91 DAY BILL", 4.5518, 4.6042)
    assert (r.kind, r.tenor_days, r.maturity_date, r.gfim_tenor) == ("bill", 91, date(2027, 1, 11), "91-DAY BILL")


def test_notes_and_bonds_mature_in_years():
    r = parse_row(["07 Sep 2026", "2023", "4 YR FXR BOND", "12.0000", "12.0000"])
    assert (r.kind, r.tenor_days, r.maturity_date, r.gfim_tenor) == ("bond", None, date(2030, 9, 7), None)
    assert parse_row(["25 Nov 2013", "1355", "2 yr fxr  note", "18", "18"]).kind == "note"


@pytest.mark.parametrize("row, error", [
    (["02 Jul 2018", "453", "56 DAY BILL", "0.0000", "0.0000"], NoRate),
    (["16 Apr 2018", "1585", "7 YR FXR BOND", "16.2500", "0.0000"], NoRate),
    (["12 Oct 2026", "2028", "91 DAY REPO", "4.5", "4.6"], ValueError),
    (["12 Oct 2026", "2028", "91 DAY BILL", "4.5"], ValueError),
    (["2026-10-12", "2028", "91 DAY BILL", "4.5", "4.6"], ValueError),
    (["12 Oct 2026", "2028", "91 DAY BILL", "-4.5", "4.6"], ValueError),
])
def test_parse_row_rejects(row, error):
    with pytest.raises(error):
        parse_row(row)


# ---------------------------------------------------------------- the BoG client

def _client(rows, page_size=3):
    """A client whose requests are answered from `rows` (newest first),
    as BoG's table would."""
    client = BogAuctionClient()
    client.page_size = page_size
    requests = []

    def fake(url, data=None):
        if data is None:
            assert url == PAGE_URL
            return b'<input id="wdtNonceFrontendServerSide_2" type="hidden" value="abc123">'
        assert url == client.table_url
        form = parse_qs(data.decode())
        assert form["wdtNonce"] == ["abc123"]
        start, length = int(form["start"][0]), int(form["length"][0])
        requests.append(start)
        return json.dumps({"data": rows[start:start + length]}).encode()

    client._request = fake
    return client, requests


ROWS = [
    ["12 Oct 2026", "2028", "91 DAY BILL", "4.5518", "4.6042"],
    ["12 Oct 2026", "2028", "182 DAY BILL", "6.0880", "6.2791"],
    ["05 Oct 2026", "2027", "91 DAY BILL", "4.5881", "4.6413"],
    ["05 Oct 2026", "2027", "182 DAY BILL", "0.0000", "0.0000"],   # no rate: skipped quietly
    ["28 Sep 2026", "2026", "91 DAY BILL", "4.6244", "4.6785"],
    ["07 Sep 2026", "2023", "4 YR FXR BOND", "12.0000", "12.0000"],
    ["31 Aug 2026", "2022", "91 DAY BILL", "4.7", "4.75"],
]


def test_client_fetches_everything_without_since():
    client, requests = _client(ROWS)
    got = []
    assert client.fetch_since(None, got.extend) == 6
    assert requests == [0, 3, 6]
    assert [r.issue_date.isoformat() for r in got][:2] == ["2026-10-12", "2026-10-12"]


def test_client_stops_once_past_since():
    client, requests = _client(ROWS)
    got = []
    client.fetch_since(date(2026, 10, 5), got.extend)
    assert requests == [0, 3]
    assert {r.issue_date for r in got} == {date(2026, 10, 12), date(2026, 10, 5)}


# ---------------------------------------------------------------- the tracker

class _Source:
    def __init__(self, results):
        self.results = results
        self.calls = []
        self.fail = None

    def fetch_since(self, since, on_page):
        self.calls.append(since)
        if self.fail:
            raise self.fail
        wanted = [r for r in self.results if since is None or r.issue_date >= since]
        on_page(wanted)
        return len(wanted)


HISTORY = [
    _r("2026-09-28", rate=4.6785, tender=2026), _r("2026-10-05", rate=4.6413, tender=2027),
    _r("2026-10-12", rate=4.6042, tender=2028),
    _r("2026-10-12", "182 DAY BILL", 6.2791, 2028), _r("2026-10-12", "364 DAY BILL", 9.7523, 2028),
    _r("2026-10-05", "364 DAY BILL", 9.8018, 2027),
    _r("2026-09-07", "4 YR FXR BOND", 12.0, 2023),
]


@pytest.fixture
def tracker(tmp_path):
    source = _Source(HISTORY)
    t = AuctionTracker(AuctionStore(tmp_path / "a.db"), source)
    assert asyncio.run(t.refresh())
    return t, source


def test_first_refresh_pulls_everything_then_only_recent(tracker):
    t, source = tracker
    assert source.calls == [None]
    assert t.as_of == date(2026, 10, 12) and t.describe()["results"] == len(HISTORY)
    asyncio.run(t.refresh())
    assert source.calls[-1] == date(2026, 10, 12).fromordinal(date(2026, 10, 12).toordinal() - REFETCH_DAYS)


def test_a_failed_refresh_keeps_what_is_stored(tracker):
    t, source = tracker
    source.fail = OSError("bog.gov.gh unreachable")
    assert not asyncio.run(t.refresh())
    assert t.as_of == date(2026, 10, 12)
    assert "unreachable" in t.describe()["last_error"]


def test_lookups(tracker):
    t, _ = tracker
    assert t.securities() == ["91 DAY BILL", "182 DAY BILL", "364 DAY BILL", "4 YR FXR BOND"]
    assert [r.issue_date.isoformat() for r in t.history("91 DAY BILL")] == ["2026-09-28", "2026-10-05", "2026-10-12"]
    latest = t.latest("91 DAY BILL")
    assert t.previous(latest).interest_rate == 4.6413
    assert t.previous(t.history("91 DAY BILL")[0]) is None
    assert t.results(since=date(2026, 10, 12))[0].issue_date == date(2026, 10, 12)


# ---------------------------------------------------------------- next auction

def test_next_auction_is_friday_for_monday_issue():
    cal = _Weekdays()
    assert next_auction(date(2026, 10, 12), 2028, date(2026, 10, 14), cal) == (
        2029, date(2026, 10, 16), date(2026, 10, 19), "scheduled")
    assert next_auction(date(2026, 10, 12), 2028, date(2026, 10, 16), cal)[3] == "today"
    assert next_auction(date(2026, 10, 12), 2028, date(2026, 10, 18), cal)[3] == "results_pending"


def test_next_auction_moves_round_holidays():
    # Friday a holiday: auction Thursday. Monday a holiday: issue Tuesday.
    cal = _Weekdays("2026-10-16", "2026-10-19")
    assert next_auction(date(2026, 10, 12), 2028, date(2026, 10, 13), cal)[:3] == (
        2029, date(2026, 10, 15), date(2026, 10, 20))


def test_next_auction_steps_on_when_bog_goes_quiet():
    # Latest held is 12-Oct, but it's 3 November: don't show 16-Oct.
    tender, auction, issue, status = next_auction(date(2026, 10, 12), 2028, date(2026, 11, 3), _Weekdays())
    assert (tender, auction, issue, status) == (2031, date(2026, 10, 30), date(2026, 11, 2), "results_pending")


# ---------------------------------------------------------------- the monitor

def _bill(symbol, tenor, maturity, y):
    return TreasuryBillQuote(symbol=symbol, description=f"GOG-BL-{symbol}", tenor=tenor,
                             maturity_date=date.fromisoformat(maturity), days_to_maturity=0,
                             closing_yield=y, closing_price=99.0, volume=1_000_000, trade_count=2)


def test_an_auction_finds_its_gfim_bill_by_tenor_and_maturity():
    r = _r("2026-10-12")
    bills = [_bill("A", "182-DAY BILL", "2027-01-11", 5.0), _bill("B", "91-DAY BILL", "2027-01-11", 4.9)]
    assert gfim_bill(r, bills).symbol == "B"
    assert gfim_bill(_r("2026-10-12", "4 YR FXR BOND"), bills) is None


def test_monitor(tracker):
    t, _ = tracker
    bills = [
        _bill("NEW91", "91-DAY BILL", "2027-01-11", 4.9581),
        _bill("OLD", "182-DAY BILL", "2026-10-19", 5.1),     # matures on the next issue date
    ]
    m = monitor(t, bills, date(2026, 10, 16), date(2026, 10, 16), _Weekdays(), "src", "url")

    assert [(r.security, r.change_bp) for r in m.latest] == [
        ("91 DAY BILL", -3.7), ("182 DAY BILL", None), ("364 DAY BILL", -5.0)]
    first = m.new_issues[0]
    assert (first.auction.security, first.gfim.symbol, first.secondary_spread_bp) == ("91 DAY BILL", "NEW91", 35.4)
    assert all(n.gfim is None for n in m.new_issues[1:] if n.auction.issue_date != date(2026, 10, 12)
               or n.auction.security != "91 DAY BILL")
    # Newest tender first, bills by tenor; bonds left out.
    assert [n.auction.issue_date for n in m.new_issues] == sorted((n.auction.issue_date for n in m.new_issues), reverse=True)
    assert all(n.auction.kind == "bill" for n in m.new_issues)

    n = m.next_auction
    assert (n.tender, n.expected_auction_date, n.expected_issue_date, n.status) == (
        2029, date(2026, 10, 16), date(2026, 10, 19), "today")
    assert [b.symbol for b in n.maturing] == ["OLD"]
    assert m.last_bond_auction.security == "4 YR FXR BOND"


# ---------------------------------------------------------------- API and page

def test_auction_api(client):
    from app import main

    # Nothing pulled (AUCTION_REFRESH_SECONDS=0 in tests): 503, not a crash.
    if main.auctions.as_of is None:
        assert client.get("/auctions/monitor").status_code == 503
        assert client.get("/auctions/results").status_code == 503

    main.auctions._store.upsert(HISTORY)
    main.auctions.reload()

    m = client.get("/auctions/monitor").json()
    assert m["as_of"] == "2026-10-12" and m["latest"] and "next_auction" in m
    assert m["gfim_report_date"]

    rows = client.get("/auctions/results").json()
    assert rows[0]["issue_date"] == "2026-10-12" and len(rows) == len(HISTORY)
    one = client.get("/auctions/results", params={"security": "91 day  bill"}).json()
    assert [r["issue_date"] for r in one] == ["2026-10-12", "2026-10-05", "2026-09-28"]
    assert one[0]["change_bp"] == -3.7 and one[-1]["change_bp"] is None
    assert len(client.get("/auctions/results", params={"limit": 2}).json()) == 2
    assert client.get("/auctions/results", params={"security": "30 DAY BILL"}).status_code == 400

    assert "auctions" in client.get("/health").json()


def test_auctions_page_is_served_and_linked(client):
    page = client.get("/auctions").text
    assert "/auctions/monitor" in page and 'href="/auctions" aria-current="page"' in page
    for path in ("/fixed-income", "/yield-curve", "/graph", "/ticker"):
        assert 'href="/auctions"' in client.get(path).text
