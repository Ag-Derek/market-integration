"""Commodity reference prices: reading the World Bank's Pink Sheet
workbook (against small workbooks built here in its layout, never the
network), storage, refresh and lookups, the cedi conversion, and the
/commodities endpoints and CMD: series on /graph."""

import io
import logging
import zipfile
from datetime import date, timedelta
from xml.sax.saxutils import escape

import pytest

from app.commodities import COMMODITIES, Price, Release, monthly_means, month_key, parse_month
from app.commodities import worldbank
from app.commodities.prices import CommodityPrices
from app.commodities.store import CommodityStore
from app.commodities.worldbank import WorldBankClient, WorldBankError, monthly_url, parse_monthly
from app.fx import Fix
from app.models.fx import FxRate

# ---------------------------------------------------------------- workbooks

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_WORKSHEET = _RELS + "/worksheet"


def _col(i):
    return chr(ord("A") + i)


def _sheet_xml(rows, strings):
    out = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, v in enumerate(row):
            ref = f"{_col(c)}{r}"
            if v is None:
                continue
            if isinstance(v, (int, float)):
                cells.append(f'<c r="{ref}"><v>{v}</v></c>')
            else:
                strings.append(v)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        out.append(f'<row r="{r}">{"".join(cells)}</row>')
    return f'<worksheet xmlns="{_MAIN}"><sheetData>{"".join(out)}</sheetData></worksheet>'


def workbook(rows, sheet_name="Monthly Prices"):
    """An .xlsx shaped like the Pink Sheet: a decoy sheet first, then
    `rows` on `sheet_name`, strings shared as Excel writes them. One sheet
    is addressed relative to xl/, the other absolutely, as both occur."""
    strings = []
    decoy = _sheet_xml([["Mismatch Details"]], strings)
    prices = _sheet_xml(rows, strings)
    sst = "".join(f"<si><t>{escape(s)}</t></si>" for s in strings)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", (
            f'<workbook xmlns="{_MAIN}" xmlns:r="{_RELS}"><sheets>'
            '<sheet name="Mismatch Details" sheetId="1" r:id="rId1"/>'
            f'<sheet name="{sheet_name}" sheetId="2" r:id="rId2"/>'
            '</sheets></workbook>'
        ))
        z.writestr("xl/_rels/workbook.xml.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_WORKSHEET}" Target="worksheets/sheet1.xml"/>'
            f'<Relationship Id="rId2" Type="{_WORKSHEET}" Target="/xl/worksheets/sheet2.xml"/>'
            '</Relationships>'
        ))
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{_MAIN}">{sst}</sst>')
        z.writestr("xl/worksheets/sheet1.xml", decoy)
        z.writestr("xl/worksheets/sheet2.xml", prices)
    return buf.getvalue()


PINK = [
    ["World Bank Commodity Price Data (The Pink Sheet)"],
    ["monthly prices in nominal US dollars, 1960 to present"],
    [],
    ["Updated on October 02, 2026"],
    [None, "Crude oil, Brent", "Cocoa", "Rubber, TSR20 **", "Urea "],
    [None, "($/bbl)", "($/kg)", "($/kg)", "($/mt)"],
    ["2026M07", 83.4, 5.61, 2.14, 400],
    ["2026M08", 90.9, "…", 2.24, 390],
    ["2026M09", 116.8, 5.92, "...", 407.5],
]


def by_series(prices):
    out = {}
    for p in prices:
        out.setdefault(p.series, []).append(p)
    return out


# ---------------------------------------------------------------- parsing

def test_parse_reads_names_units_and_months():
    release = parse_monthly(workbook(PINK))
    assert release.updated_on == date(2026, 10, 2)
    series = by_series(release.prices)
    # Footnote marks and trailing spaces dropped; units without brackets.
    assert set(series) == {"Crude oil, Brent", "Cocoa", "Rubber, TSR20", "Urea"}
    brent = series["Crude oil, Brent"]
    assert [(p.month, p.value, p.unit, p.source) for p in brent] == [
        (date(2026, 7, 1), 83.4, "$/bbl", "worldbank"),
        (date(2026, 8, 1), 90.9, "$/bbl", "worldbank"),
        (date(2026, 9, 1), 116.8, "$/bbl", "worldbank"),
    ]
    assert series["Urea"][0].unit == "$/mt"


def test_months_not_priced_are_skipped_quietly(caplog):
    with caplog.at_level(logging.WARNING):
        series = by_series(parse_monthly(workbook(PINK)).prices)
    assert [p.month.month for p in series["Cocoa"]] == [7, 9]          # "…"
    assert [p.month.month for p in series["Rubber, TSR20"]] == [7, 8]  # "..."
    assert not caplog.records


def test_cells_that_arent_prices_are_skipped_with_one_warning(caplog):
    rows = PINK[:6] + [["2026M07", "#VALUE!", 0, -1, 400], ["2026M08", 90.9, 5.0, 2.0, 390]]
    with caplog.at_level(logging.WARNING):
        series = by_series(parse_monthly(workbook(rows)).prices)
    assert [p.value for p in series["Crude oil, Brent"]] == [90.9]
    assert [p.value for p in series["Cocoa"]] == [5.0]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "Skipped 3" in warnings[0].getMessage()


def test_a_release_without_an_updated_line_still_parses():
    release = parse_monthly(workbook([row for row in PINK if "Updated" not in str(row[0] if row else "")]))
    assert release.updated_on is None and release.prices


@pytest.mark.parametrize("rows, sheet", [
    (PINK, "Monthly Indices"),                                           # no Monthly Prices sheet
    ([r for r in PINK if not (r and str(r[0]).startswith("2026"))], "Monthly Prices"),  # no months
    ([r for r in PINK if not (len(r) > 1 and str(r[1]).startswith("("))], "Monthly Prices"),  # no units row
    (PINK[:6] + [["2026M07", "…", "…", "…", "…"]], "Monthly Prices"),   # no prices at all
    (PINK[:4] + [[None, "Cocoa", "Cocoa"], [None, "($/kg)", "($/kg)"], ["2026M07", 1, 2]], "Monthly Prices"),
])
def test_a_workbook_laid_out_otherwise_is_an_error(rows, sheet):
    with pytest.raises(WorldBankError):
        parse_monthly(workbook(rows, sheet_name=sheet))


def test_something_that_isnt_a_workbook_is_an_error():
    with pytest.raises(WorldBankError):
        parse_monthly(b"<html>not found</html>")


def test_the_monthly_link_is_read_off_the_page():
    url = ("https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/"
           "related/CMO-Historical-Data-Monthly.xlsx")
    page = (f'<a href="{url.replace("Monthly", "Annual")}">Annual prices</a>'
            f'<a class="x" href="{url}">Monthly prices</a>')
    assert monthly_url(page) == url
    with pytest.raises(WorldBankError):
        monthly_url("<p>Pink Sheet moved</p>")


def test_the_client_finds_the_link_then_reads_the_workbook(monkeypatch):
    xlsx = workbook(PINK)
    calls = []

    def fake_get(url):
        calls.append(url)
        if url == worldbank.PAGE_URL:
            return b'<a href="https://example.org/doc/CMO-Historical-Data-Monthly.xlsx">Monthly</a>'
        assert url == "https://example.org/doc/CMO-Historical-Data-Monthly.xlsx"
        return xlsx

    monkeypatch.setattr(worldbank, "_get", fake_get)
    release = WorldBankClient().fetch()
    assert len(calls) == 2 and release.updated_on == date(2026, 10, 2)
    assert len(release.prices) == 10


# ---------------------------------------------------------------- months and FX

def test_months_as_keys():
    assert month_key(date(2026, 9, 1)) == "2026-09"
    assert parse_month("2026-09") == date(2026, 9, 1)
    for bad in ["2026-9", "2026-13", "26-09", "Sep 2026", "2026-09-01"]:
        with pytest.raises(ValueError):
            parse_month(bad)


def usd_fix(day, mid):
    return FxRate(currency="USD", date=day, mid=mid, buying=mid - 0.01, selling=mid + 0.01, source_pair="USDGHS")


def test_monthly_means_average_each_months_fixes():
    fixes = [usd_fix(date(2026, 8, 28), 11.0), usd_fix(date(2026, 9, 1), 12.0),
             usd_fix(date(2026, 9, 2), 12.5), usd_fix(date(2026, 9, 30), 13.0)]
    means = monthly_means(fixes)
    assert means[date(2026, 8, 1)] == (11.0, 1)
    assert means[date(2026, 9, 1)] == (pytest.approx(12.5), 3)
    assert monthly_means([]) == {}


def test_every_commodity_names_a_world_bank_series_and_a_unit():
    for code, c in COMMODITIES.items():
        assert c.code == code and code == code.upper()
        assert "worldbank" in c.series and c.unit.startswith("$/")


# ---------------------------------------------------------------- store

def price(series, month, value, unit="$/bbl", source="worldbank"):
    return Price(source, series, unit, month, value)


def test_the_store_replaces_by_source_series_and_month(tmp_path):
    db = tmp_path / "c.db"
    m = date(2026, 9, 1)
    CommodityStore(db).upsert([price("Crude oil, Brent", m, 100.0), price("Gold", m, 4000.0, "$/troy oz")])
    first = CommodityStore(db).first_fetched("worldbank", "Crude oil, Brent", m)
    CommodityStore(db).upsert([price("Crude oil, Brent", m, 116.8)])  # a revision
    restarted = CommodityStore(db)
    assert [(p.series, p.value) for p in restarted.series("worldbank", ["Crude oil, Brent", "Gold"])] == [
        ("Crude oil, Brent", 116.8), ("Gold", 4000.0)]
    assert restarted.first_fetched("worldbank", "Crude oil, Brent", m) == first
    assert restarted.series("worldbank", []) == [] and restarted.series("bog", ["Gold"]) == []


# ---------------------------------------------------------------- prices

class FakeSource:
    source = "worldbank"

    def __init__(self, release=None, error=None):
        self.release, self.error, self.calls = release, error, 0

    def fetch(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.release


def months(start, n):
    out, d = [], start
    for _ in range(n):
        out.append(d)
        d = (d + timedelta(days=32)).replace(day=1)
    return out


async def test_refresh_stores_the_release_and_indexes_the_commodities_offered(tmp_path):
    prices = [price("Crude oil, Brent", m, 80.0 + i) for i, m in enumerate(months(date(2025, 1, 1), 21))]
    prices.append(price("Coal, Australian", date(2026, 9, 1), 147.1, "$/mt"))  # stored, not offered
    store = CommodityStore(tmp_path / "c.db")
    cp = CommodityPrices(store, [FakeSource(Release(date(2026, 10, 2), prices))])
    assert await cp.refresh() is True
    assert cp.commodities() == ["BRENT"] and cp.has("BRENT") and not cp.has("GOLD")
    assert cp.latest("BRENT").month == date(2026, 9, 1) and cp.latest("BRENT").value == 100.0
    assert cp.as_of == date(2026, 9, 1)
    assert cp.on("BRENT", date(2025, 6, 1)).value == 85.0
    assert cp.on("BRENT", date(2024, 12, 1)) is None
    assert [p.month for p in cp.history("BRENT", date(2026, 7, 1))] == months(date(2026, 7, 1), 3)
    assert len(cp.history("BRENT")) == 21 and cp.history("GOLD") == []
    assert store.series("worldbank", ["Coal, Australian"])  # kept for later
    status = cp.describe()
    assert status["as_of"] == "2026-09" and status["commodities"] == 1
    assert status["sources"]["worldbank"]["release"] == "2026-10-02"
    assert status["sources"]["worldbank"]["last_error"] is None


async def test_a_failed_refresh_keeps_what_is_stored(tmp_path):
    store = CommodityStore(tmp_path / "c.db")
    store.upsert([price("Gold", date(2026, 9, 1), 4319.0, "$/troy oz")])
    cp = CommodityPrices(store, [FakeSource(error=WorldBankError("page down"))])
    assert await cp.refresh() is False
    assert cp.latest("GOLD").value == 4319.0
    assert "page down" in cp.describe()["sources"]["worldbank"]["last_error"]


def test_a_series_in_an_unexpected_unit_is_not_offered(tmp_path):
    # Cocoa in $/mt would be off by 1,000 against the $/kg COMMODITIES expects.
    store = CommodityStore(tmp_path / "c.db")
    store.upsert([price("Cocoa", date(2026, 9, 1), 5920.0, "$/mt"), price("Gold", date(2026, 9, 1), 4319.0, "$/troy oz")])
    cp = CommodityPrices(store, [])
    cp.reload()
    assert cp.commodities() == ["GOLD"]
    assert "expected $/kg" in cp.describe()["unit_mismatches"]["COCOA"]


# ---------------------------------------------------------------- API
# `client` is the session-wide running app; COMMODITY_REFRESH_SECONDS=0
# there, so these store their own prices in its database. Two years of
# 2018-19 data, with USD fixes on weekdays only (test_fx's own fixture
# overwrites any it shares), each month's mid constant so means are exact.

API_MONTHS = months(date(2018, 1, 1), 24)
GOLD = {m: 1300.0 + 10 * i for i, m in enumerate(API_MONTHS)}
BRENT = {m: 60.0 + i for i, m in enumerate(API_MONTHS) if m != date(2019, 6, 1)}  # June 2019 not priced
USD_MID = {m: 5.0 + 0.1 * i for i, m in enumerate(API_MONTHS)}


def weekdays_in(month):
    d = month
    while d.month == month.month:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


@pytest.fixture(scope="module")
def app_prices(client):
    from app.main import commodity_prices, fx_rates

    commodity_prices._store.upsert(
        [price("Gold", m, v, "$/troy oz") for m, v in GOLD.items()] +
        [price("Crude oil, Brent", m, v) for m, v in BRENT.items()]
    )
    commodity_prices.reload()
    fx_rates._store.upsert([Fix(d, "US Dollar", "USDGHS", mid - 0.01, mid + 0.01, mid)
                            for m, mid in USD_MID.items() for d in weekdays_in(m)])
    fx_rates.reload()
    return commodity_prices


def test_prices_give_the_latest_month_in_dollars_and_cedis(client, app_prices):
    body = client.get("/commodities/prices").json()
    assert body["as_of"] == "2019-12" and body["month"] is None
    assert body["currency"] == "USD" and body["base"] == "GHS"
    assert body["sources"]["worldbank"]["url"].startswith("https://www.worldbank.org/")
    rows = {r["code"]: r for r in body["commodities"]}
    assert list(rows) == ["BRENT", "GOLD"]  # COMMODITIES order
    dec, nov, dec_before = date(2019, 12, 1), date(2019, 11, 1), date(2018, 12, 1)
    gold = rows["GOLD"]
    assert (gold["month"], gold["usd"], gold["unit"], gold["per"]) == ("2019-12", GOLD[dec], "$/troy oz", "troy oz")
    assert (gold["previous_month"], gold["previous_usd"]) == ("2019-11", GOLD[nov])
    assert gold["change_percent"] == pytest.approx((GOLD[dec] / GOLD[nov] - 1) * 100)
    assert gold["year_ago_month"] == "2018-12"
    assert gold["yoy_percent"] == pytest.approx((GOLD[dec] / GOLD[dec_before] - 1) * 100)
    assert gold["ghs_per_usd"] == pytest.approx(USD_MID[dec])
    assert gold["fx_fixes"] == len(list(weekdays_in(dec)))
    assert gold["ghs"] == pytest.approx(GOLD[dec] * USD_MID[dec])
    assert gold["ghs_yoy_percent"] == pytest.approx(
        (GOLD[dec] * USD_MID[dec] / (GOLD[dec_before] * USD_MID[dec_before]) - 1) * 100)


def test_prices_for_a_month_take_the_one_in_force(client, app_prices):
    body = client.get("/commodities/prices?month=2019-06").json()
    rows = {r["code"]: r for r in body["commodities"]}
    assert rows["GOLD"]["month"] == "2019-06"
    assert rows["BRENT"]["month"] == "2019-05"  # June not priced: May's is in force
    assert body["month"] == "2019-06" and body["as_of"] == "2019-06"
    # The month after a gap compares with the last month priced.
    july = {r["code"]: r for r in client.get("/commodities/prices?month=2019-07").json()["commodities"]}
    assert july["BRENT"]["previous_month"] == "2019-05"


@pytest.mark.parametrize("query, status", [
    ("month=2017-12", 404),   # before the history
    ("month=2019-13", 400),
    ("month=June", 400),
])
def test_prices_reject_months_they_cant_answer(client, app_prices, query, status):
    assert client.get("/commodities/prices?" + query).status_code == status


def test_prices_before_the_first_pull_are_a_503(client, app_prices, monkeypatch):
    monkeypatch.setattr(app_prices, "_index", {})
    res = client.get("/commodities/prices")
    assert res.status_code == 503 and "No commodity prices yet" in res.json()["detail"]["message"]


def test_history_gives_each_month_in_dollars_and_cedis(client, app_prices):
    body = client.get("/commodities/gold/history?since=2019-01").json()
    assert (body["code"], body["unit"], body["series"], body["source"]) == ("GOLD", "$/troy oz", "Gold", "worldbank")
    assert [p["month"] for p in body["points"]] == [month_key(m) for m in API_MONTHS[12:]]
    first = body["points"][0]
    assert first["usd"] == GOLD[date(2019, 1, 1)]
    assert first["ghs"] == pytest.approx(GOLD[date(2019, 1, 1)] * USD_MID[date(2019, 1, 1)])
    assert len(client.get("/commodities/GOLD/history").json()["points"]) == 24


@pytest.mark.parametrize("path, status", [
    ("/commodities/PLUTONIUM/history", 400),   # not offered
    ("/commodities/COCOA/history", 503),       # offered, no prices held
    ("/commodities/GOLD/history?since=2019", 400),
])
def test_history_errors(client, app_prices, path, status):
    assert client.get(path).status_code == status


def test_commodities_graph_as_monthly_series(client, app_prices):
    body = client.get("/graph/series?series=CMD:GOLD,cmd:brent&range=Max").json()
    gold, brent = body["series"]
    assert gold["id"] == "CMD:GOLD" and brent["id"] == "CMD:BRENT"
    assert (gold["kind"], gold["unit"], gold["label"]) == ("commodity", "US$ per troy oz", "Gold")
    assert gold["points"][0][:2] == ["2018-01-01", GOLD[date(2018, 1, 1)]]
    assert gold["last"]["date"] == "2019-12-01" and len(gold["points"]) == 24
    assert client.get("/graph/series?series=CMD:NOPE").status_code == 400


def test_health_reports_the_commodity_prices(client, app_prices):
    body = client.get("/health").json()
    assert body["commodities"]["as_of"] == "2019-12" and "worldbank" in body["commodities"]["sources"]


def test_the_commodities_page_is_served(client):
    page = client.get("/commodities")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert 'href="/commodities" aria-current="page"' in page.text
    assert "/commodities/prices" in page.text


@pytest.mark.parametrize("path", ["/ticker", "/fixed-income", "/yield-curve", "/auctions", "/fx",
                                  "/calendar", "/graph", "/tickets", "/hedging"])
def test_every_tabbed_page_links_the_commodities_tab(client, path):
    assert '<a class="tab" href="/commodities">Commodities</a>' in client.get(path).text
