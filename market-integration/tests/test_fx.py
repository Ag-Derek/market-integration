"""Currency conversion at Bank of Ghana rates: parsing and normalizing
BoG's rows, the client that pulls them (against canned replies shaped
like BoG's, never the network), storage, refresh and lookups, and the
/fx endpoints and /candles?currency=."""

import json
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs

import pytest

from app.fx import CURRENCIES, PAIRS, Fix, normalize, parse_row
from app.fx.bog import HISTORY_PAGE_URL, PAGE_SIZE, TABLE_URL, BankOfGhanaClient, BogError
from app.fx.rates import FxRates
from app.fx.store import FxStore

TODAY = date(2026, 10, 6)  # a Tuesday


def fix(day, pair="USDGHS", mid=11.765, spread=0.006, name="US Dollar"):
    return Fix(day, name, pair, round(mid - spread, 6), round(mid + spread, 6), mid)


def weekdays(start, end):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


# ---------------------------------------------------------------- parsing

def test_parse_row_reads_bogs_format():
    f = parse_row(["05 Oct 2026", "US Dollar", "USDGHS", "11.7591", "11.7709", "11.7650"])
    assert f == Fix(date(2026, 10, 5), "US Dollar", "USDGHS", 11.7591, 11.7709, 11.765)
    # Old rows carry thousands separators.
    assert parse_row(["03 Jan 1996", "US Dollar", "USDGHS", "1,437.2700", "1,464.3600", "1,450.8150"]).mid == 1450.815


@pytest.mark.parametrize("row", [
    ["05 Oct 2026", "US Dollar", "USDGHS", "11.7591", "11.7709"],           # a column short
    ["2026-10-05", "US Dollar", "USDGHS", "11.7591", "11.7709", "11.7650"],  # another date format
    ["05 Oct 2026", "US Dollar", "USDGHS", "11.7591", "0", "11.7650"],       # a zero rate
    ["05 Oct 2026", "US Dollar", "USDGHS", "n/a", "11.7709", "11.7650"],
])
def test_parse_row_rejects_anything_else(row):
    with pytest.raises(ValueError):
        parse_row(row)


def test_normalize_gives_cedis_per_unit_either_way_round():
    usd = normalize(parse_row(["05 Oct 2026", "US Dollar", "USDGHS", "11.7591", "11.7709", "11.7650"]))
    assert (usd.currency, usd.mid, usd.buying, usd.selling) == ("USD", 11.765, 11.7591, 11.7709)
    assert usd.per_ghs == pytest.approx(1 / 11.765)

    # GHSNGN is naira per cedi: inverted, and buying/selling swap so
    # buying stays the lower.
    ngn = normalize(parse_row(["05 Oct 2026", "Naira", "GHSNGN", "112.8219", "113.0769", "112.9494"]))
    assert ngn.currency == "NGN" and ngn.source_pair == "GHSNGN"
    assert ngn.per_ghs == pytest.approx(112.9494)
    assert ngn.mid == pytest.approx(1 / 112.9494)
    assert ngn.buying == pytest.approx(1 / 113.0769) and ngn.selling == pytest.approx(1 / 112.8219)
    assert ngn.buying < ngn.selling


@pytest.mark.parametrize("pair", ["GHSSLL", "GHSMRO", "WAUGHS", "XYZGHS"])
def test_pairs_we_dont_offer_normalize_to_nothing(pair):
    # Leone (rescaled under its old code), ouguiya (a withdrawn unit),
    # the ECOWAS unit of account, and anything unknown.
    assert normalize(fix(TODAY, pair=pair, mid=2.0)) is None


def test_every_offered_currency_has_one_bog_pair():
    assert sorted(PAIRS.values()) == sorted(CURRENCIES)
    assert PAIRS["USDGHS"] == "USD" and PAIRS["GHSXOF"] == "XOF" and PAIRS["GHSGMD"] == "GMD"


# ---------------------------------------------------------------- BoG client
# BoG's table is POSTed DataTables parameters and answers JSON rows,
# newest first; the request needs a nonce from the history page.

PAGE_HTML = (
    '<input type="hidden" id="wdtNonceFrontendServerSide_40" '
    'name="wdtNonceFrontendServerSide_40" value="abc123" />'
)


class FakeBog:
    """Stands in for BankOfGhanaClient._request: serves the history
    page, and table pages from `rows` (newest first) by the requested
    start. `expired` nonces get BoG's empty 200."""

    def __init__(self, rows, expired=()):
        self.rows = rows
        self.expired = set(expired)
        self.nonce_reads = 0
        self.requests = []

    def __call__(self, url, data=None):
        if url == HISTORY_PAGE_URL:
            self.nonce_reads += 1
            return PAGE_HTML.replace("abc123", f"nonce{self.nonce_reads}").encode()
        assert url == TABLE_URL
        form = {k: v[0] for k, v in parse_qs(data.decode(), keep_blank_values=True).items()}
        self.requests.append(form)
        if form["wdtNonce"] in self.expired:
            return b""
        start, length = int(form["start"]), int(form["length"])
        return json.dumps({"draw": 1, "data": self.rows[start:start + length]}).encode()


def bog_rows(days, pairs=("USDGHS", "GHSNGN")):
    return [
        [d.strftime("%d %b %Y"), "x", p, "1.0", "1.2", "1.1"]
        for d in sorted(days, reverse=True) for p in pairs
    ]


def client_with(fake):
    client = BankOfGhanaClient()
    client._request = fake
    return client


def test_client_pages_newest_first_and_stops_past_since():
    days = list(weekdays(date(2026, 1, 1), TODAY))
    fake = FakeBog(bog_rows(days) * 1)
    got = []
    since = date(2026, 9, 1)
    n = client_with(fake).fetch_since(since, got.extend)
    assert n == len(got) == 2 * len([d for d in days if d >= since])
    assert min(f.date for f in got) == date(2026, 9, 1)
    form = fake.requests[0]
    assert (form["order[0][column]"], form["order[0][dir]"], form["wdtNonce"]) == ("0", "desc", "nonce1")
    assert form["length"] == str(PAGE_SIZE)


def test_client_reads_more_pages_until_it_reaches_since():
    days = list(weekdays(date(2015, 1, 1), TODAY))  # ~3,000 days x 2 pairs: several pages
    fake = FakeBog(bog_rows(days))
    pages = []
    n = client_with(fake).fetch_since(date(2016, 1, 1), pages.append)
    assert len(pages) > 1 and n == sum(len(p) for p in pages)
    assert [int(r["start"]) for r in fake.requests] == [i * PAGE_SIZE for i in range(len(fake.requests))]
    assert min(f.date for p in pages for f in p) == date(2016, 1, 1)


def test_an_expired_nonce_is_read_again_once():
    fake = FakeBog(bog_rows([TODAY]), expired={"nonce1"})
    got = []
    client_with(fake).fetch_since(TODAY, got.extend)
    assert fake.nonce_reads == 2 and len(got) == 2


def test_a_reply_that_is_never_the_table_is_an_error():
    fake = FakeBog(bog_rows([TODAY]), expired={"nonce1", "nonce2"})
    with pytest.raises(BogError, match="didn't answer with rows"):
        client_with(fake).fetch_since(TODAY, lambda fixes: None)


def test_a_page_without_the_nonce_is_an_error():
    client = BankOfGhanaClient()
    client._request = lambda url, data=None: b"<html>maintenance</html>"
    with pytest.raises(BogError, match="no wdtNonce"):
        client.fetch_since(TODAY, lambda fixes: None)


def test_bad_rows_are_skipped_not_fatal():
    rows = bog_rows([TODAY]) + [["garbage"], [TODAY.strftime("%d %b %Y"), "x", "USDGHS", "0", "1", "1"]]
    got = []
    client_with(FakeBog(rows)).fetch_since(TODAY, got.extend)
    assert len(got) == 2


# ---------------------------------------------------------------- store, refresh, lookups

class FakeSource:
    """fetch_since over `fixes`, newest first, `page` at a time; raises
    after `fail_after` pages if set."""

    def __init__(self, fixes, page=5, fail_after=None):
        self.fixes = sorted(fixes, key=lambda f: f.date, reverse=True)
        self.page = page
        self.fail_after = fail_after
        self.calls = []

    def fetch_since(self, since, on_page):
        self.calls.append(since)
        rows = [f for f in self.fixes if f.date >= since]
        for n, i in enumerate(range(0, len(rows), self.page)):
            if self.fail_after is not None and n >= self.fail_after:
                raise BogError("BoG is down")
            on_page(rows[i:i + self.page])
        return len(rows)


def history(start, end, mid=lambda d: 10 + d.toordinal() % 7 / 10):
    return [fix(d, mid=mid(d)) for d in weekdays(start, end)] + \
           [fix(d, pair="GHSNGN", mid=100.0, name="Naira") for d in weekdays(start, end)]


@pytest.fixture
def rates(tmp_path):
    def make(fixes, **source_args):
        source = FakeSource(fixes, **source_args)
        r = FxRates(FxStore(tmp_path / "fx.db"), source, history_years=1)
        return r, source
    return make


async def test_first_refresh_backfills_then_only_new_days(rates):
    r, source = rates(history(date(2025, 9, 1), TODAY))
    assert await r.refresh(today=TODAY)
    start = r.history_start(TODAY)
    assert source.calls == [start]
    assert r.currencies() == ["USD", "NGN"] and r.as_of == TODAY
    assert r.on("USD", start - timedelta(days=30)).date >= start  # nothing older kept

    assert await r.refresh(today=TODAY)
    assert source.calls[-1] == TODAY  # from the latest stored day on


async def test_a_failed_pull_keeps_what_it_got_and_backfills_again(rates):
    r, source = rates(history(date(2025, 9, 1), TODAY), page=10, fail_after=2)
    assert not await r.refresh(today=TODAY)
    assert "BoG is down" in r.describe()["last_error"]
    assert r.as_of == TODAY  # the newest pages landed before it failed

    source.fail_after = None
    assert await r.refresh(today=TODAY)
    assert source.calls[-1] == r.history_start(TODAY)  # history incomplete: from the start again
    assert r.describe()["last_error"] is None and r.describe()["last_success_at"]


async def test_a_first_backfill_serves_the_latest_fixes_after_its_first_page(rates):
    r, source = rates(history(date(2025, 9, 1), TODAY), page=10)
    seen = []
    store_upsert = r._store.upsert

    def upsert(fixes):
        store_upsert(fixes)
        seen.append(r.as_of)  # what lookups saw before this page's reload
    r._store.upsert = upsert
    await r.refresh(today=TODAY)
    assert seen[0] is None and seen[1] == TODAY  # usable from the second page on, not only at the end


async def test_close_stops_a_pull_at_its_next_page(rates):
    r, source = rates(history(date(2025, 9, 1), TODAY), page=10)
    r.close()
    assert not await r.refresh(today=TODAY)
    assert r.as_of is None


async def test_lookups(rates):
    r, _ = rates([fix(date(2026, 10, 1), mid=11.7), fix(date(2026, 10, 2), mid=11.8), fix(date(2026, 10, 5), mid=11.9)])
    await r.refresh(today=TODAY)
    assert r.latest("USD").mid == 11.9
    assert r.on("USD", date(2026, 10, 4)).date == date(2026, 10, 2)  # Sunday: Friday's
    assert r.on("USD", date(2026, 10, 5)).mid == 11.9
    assert r.on("USD", date(2020, 1, 1)).date == date(2026, 10, 1)   # before the history: the earliest
    assert r.per_ghs("USD") == pytest.approx(1 / 11.9)
    assert r.per_ghs("GHS") == 1.0 and r.has("GHS")
    assert r.per_ghs("EUR") is None and not r.has("EUR")


def test_the_scheduled_pull_fills_the_database_and_reports_by_exit_code(tmp_path, monkeypatch):
    # What the daily scheduled task runs (scripts/register-fx-pull-task.ps1).
    from app import config
    from app.fx import pull

    source = FakeSource(history(date(2026, 9, 1), TODAY))
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "market_data.db")
    monkeypatch.setattr(pull, "BankOfGhanaClient", lambda: source)
    log = tmp_path / "fx-pull.log"
    assert pull.main(["--log", str(log)]) == 0
    assert FxStore(tmp_path / "market_data.db").span()[1] == TODAY
    assert "FX pull done" in log.read_text(encoding="utf-8")

    source.fail_after = 0
    assert pull.main(["--log", str(log)]) == 1
    assert "FX pull failed: BogError: BoG is down" in log.read_text(encoding="utf-8")


def test_the_scheduled_pull_skips_bog_once_todays_fix_is_in(tmp_path, monkeypatch):
    # It runs at every unlock: after today's fix, that must cost nothing.
    from app import config
    from app.fx import pull

    today = datetime.now(timezone.utc).date()
    source = FakeSource([fix(today - timedelta(days=1)), fix(today)])
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "market_data.db")
    monkeypatch.setattr(pull, "BankOfGhanaClient", lambda: source)
    log = tmp_path / "fx-pull.log"
    FxStore(config.DB_PATH).upsert([fix(today - timedelta(days=1))])

    assert pull.main(["--log", str(log)]) == 0
    assert len(source.calls) == 1
    assert f"New fix: {today}, first seen" in log.read_text(encoding="utf-8")
    assert pull.main(["--log", str(log)]) == 0
    assert len(source.calls) == 1  # not asked again
    assert "Already have today's fix" in log.read_text(encoding="utf-8")


def test_refetching_a_day_keeps_when_it_was_first_seen(tmp_path):
    store = FxStore(tmp_path / "fx.db")
    store.upsert([fix(TODAY, mid=11.7)])
    first = store.first_fetched(TODAY)
    store.upsert([fix(TODAY, mid=11.8)])
    assert store.first_fetched(TODAY) == first
    assert store.all()[0].mid == 11.8
    assert store.first_fetched(TODAY - timedelta(days=1)) is None


def test_the_store_keeps_fixes_across_restarts_and_replaces_by_day_and_pair(tmp_path):
    db = tmp_path / "fx.db"
    FxStore(db).upsert([fix(TODAY, mid=11.7), fix(TODAY, pair="GHSNGN", mid=113.0)])
    FxStore(db).upsert([fix(TODAY, mid=11.8)])
    restarted = FxStore(db)
    assert sorted((f.pair, f.mid) for f in restarted.all()) == [("GHSNGN", 113.0), ("USDGHS", 11.8)]
    assert restarted.span() == (TODAY, TODAY)


# ---------------------------------------------------------------- API
# `client` is the session-wide running app; FX_REFRESH_SECONDS=0 there,
# so these store their own fixes in its database.

USD_MID = lambda d: 11 + (d - date(2015, 1, 1)).days / 1000  # noqa: E731 -- a different rate every day
NGN_PER_GHS = 110.0


@pytest.fixture(scope="module")
def app_rates(client):
    from app.main import fx_rates

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=6 * 365)
    days = list(weekdays(start, today))
    fx_rates._store.upsert(
        [fix(d, mid=USD_MID(d)) for d in days] +
        [fix(d, pair="GHSNGN", mid=NGN_PER_GHS, name="Naira") for d in days]
    )
    fx_rates.reload()
    return fx_rates


def test_rates_lists_each_currency_against_the_cedi(client, app_rates):
    body = client.get("/fx/rates").json()
    assert body["base"] == {"code": "GHS", "name": "Ghanaian cedi", "symbol": "GH₵"}
    assert body["rate"] == "mid" and "Bank of Ghana" in body["source"]
    rows = {r["code"]: r for r in body["currencies"]}
    assert set(rows) == {"USD", "NGN"}
    usd = rows["USD"]
    assert usd["date"] == body["as_of"] == app_rates.as_of.isoformat()
    assert usd["ghs_per_unit"] == pytest.approx(USD_MID(app_rates.as_of))
    assert usd["per_ghs"] == pytest.approx(1 / usd["ghs_per_unit"])
    assert (usd["symbol"], usd["source_pair"]) == ("US$", "USDGHS")
    assert rows["NGN"]["per_ghs"] == pytest.approx(NGN_PER_GHS) and rows["NGN"]["source_pair"] == "GHSNGN"


def test_rates_on_a_date_take_the_fix_in_force(client, app_rates):
    sunday = app_rates.as_of - timedelta(days=(app_rates.as_of.weekday() + 1) % 7 or 7)
    body = client.get(f"/fx/rates?date={sunday}").json()
    usd = next(r for r in body["currencies"] if r["code"] == "USD")
    assert usd["date"] == (sunday - timedelta(days=2)).isoformat()  # Friday
    assert body["date"] == sunday.isoformat()
    assert client.get("/fx/rates?date=1990-01-01").status_code == 404


def test_rates_before_the_first_pull_are_a_503(client, app_rates, monkeypatch):
    monkeypatch.setattr(app_rates, "_index", {})
    res = client.get("/fx/rates")
    assert res.status_code == 503 and "No Bank of Ghana rates yet" in res.json()["detail"]["message"]


def test_convert(client, app_rates):
    mid = USD_MID(app_rates.as_of)
    body = client.get("/fx/convert?amount=100&to=usd").json()
    assert (body["from"], body["to"]) == ("GHS", "USD")
    assert body["result"] == pytest.approx(100 / mid)
    assert body["fixes"] == {"USD": app_rates.as_of.isoformat()}
    # Between two foreign currencies, through the cedi.
    cross = client.get("/fx/convert?amount=1&from=USD&to=NGN").json()
    assert cross["rate"] == pytest.approx(mid * NGN_PER_GHS)
    # On a past date, that day's fix.
    day = app_rates.as_of - timedelta(days=60)
    day -= timedelta(days=max(0, day.weekday() - 4))
    past = client.get(f"/fx/convert?amount=1&from=USD&to=GHS&date={day}").json()
    assert past["result"] == pytest.approx(USD_MID(day))


@pytest.mark.parametrize("query,status", [
    ("amount=1&to=XYZ", 400),
    ("amount=1&to=SLL", 400),        # stored, but not offered
    ("amount=1&to=EUR", 503),        # offered, but no rate held in this test
    ("amount=1&to=USD&date=1990-01-01", 404),
])
def test_convert_errors(client, app_rates, query, status):
    assert client.get("/fx/convert?" + query).status_code == status


def _by_time(body):
    return {c["time"]: c for c in body["candles"]}


@pytest.mark.parametrize("range_", ["1Y", "5Y"])
def test_daily_and_weekly_candles_convert_at_each_days_fix(client, app_rates, range_):
    ghs = client.get(f"/candles?symbol=MTNGH&range={range_}").json()
    usd = client.get(f"/candles?symbol=MTNGH&range={range_}&currency=USD").json()
    assert (ghs["currency"], ghs["fx"]) == ("GHS", None)
    assert usd["currency"] == "USD"
    assert usd["fx"]["method"] == "daily" and usd["fx"]["rate"] == "mid"
    today = datetime.now(timezone.utc).date()
    span = timedelta(weeks=1) if usd["interval"] == "1w" else timedelta(days=1)
    plain, converted = _by_time(ghs), _by_time(usd)
    times = sorted(set(plain) & set(converted))[:-1]  # the newest is live; it may move between requests
    assert len(times) > 20
    factors = set()
    for t in times:
        last_day = (datetime.fromisoformat(t) + span - timedelta(days=1)).date()
        expected = app_rates.per_ghs("USD", min(last_day, today))
        for k in ("open", "high", "low", "close"):
            assert converted[t][k] == pytest.approx(plain[t][k] * expected)
        assert converted[t]["volume"] == plain[t]["volume"]
        factors.add(round(expected, 9))
    assert len(factors) > 1  # really per day, not one rate for all


def test_intraday_candles_convert_at_the_latest_fix(client, app_rates):
    ghs = client.get("/candles?symbol=MTNGH&range=1D").json()
    usd = client.get("/candles?symbol=MTNGH&range=1D&currency=usd").json()
    assert usd["fx"]["method"] == "latest" and usd["fx"]["as_of"] == app_rates.as_of.isoformat()
    plain, converted = _by_time(ghs), _by_time(usd)
    for t in sorted(set(plain) & set(converted))[:-1]:
        assert converted[t]["close"] == pytest.approx(plain[t]["close"] * app_rates.per_ghs("USD"))


def test_candles_in_cedis_are_unchanged(client, app_rates):
    body = client.get("/candles?symbol=MTNGH&range=1Y&currency=GHS").json()
    assert (body["currency"], body["fx"]) == ("GHS", None)


@pytest.mark.parametrize("query,status", [
    ("symbol=GHGGOG069931&currency=USD", 400),   # a bond: per 100 of face value
    ("symbol=MTNGH&currency=XYZ", 400),
])
def test_candles_currency_errors(client, app_rates, query, status):
    assert client.get("/candles?" + query).status_code == status


def test_bond_candles_have_no_currency(client):
    assert client.get("/candles?symbol=GHGGOG069931").json()["currency"] is None


def test_health_reports_fx_without_depending_on_it(client, app_rates):
    body = client.get("/health").json()
    assert body["fx"]["as_of"] == app_rates.as_of.isoformat()
    assert "fx" not in body["components"]


# ---------------------------------------------------------------- pages

def test_the_currency_script_is_served(client):
    script = client.get("/static/currency.js")
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert "window.Currency" in script.text and "/fx/rates" in script.text


@pytest.mark.parametrize("path", ["/ticker", "/stock/MTNGH"])
def test_pages_with_prices_offer_the_currency_picker(client, path):
    page = client.get(path).text
    tag = '<script src="/static/currency.js"></script>'
    assert tag in page and 'id="currency"' in page and "Currency.mount(" in page
    # Before the search bar, which formats equity prices with it.
    assert page.index(tag) < page.index('<script src="/static/search.js"></script>')
