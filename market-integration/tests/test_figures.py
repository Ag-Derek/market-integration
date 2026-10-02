import asyncio
import time
from datetime import date, timedelta

import pytest

from app.company import performance
from app.company.figures import calculated_figures, dividends_paid, with_market_cap
from app.company.index import simulated_gse_ci
from app.company.performance import Day
from app.models.company import Company, FinancialYear

TODAY = date(2026, 10, 1)
SRC = {"source": "Test fixture", "as_of": "2025-12-31"}


def sourced(value, **kw):
    return {"value": value, **SRC, **kw}


def series(closes, end=TODAY, volume=100, highs=None, lows=None):
    """Consecutive daily candles ending at `end`, from a list of closes."""
    start = end - timedelta(days=len(closes) - 1)
    return [
        Day(start + timedelta(days=n), c, (highs or {}).get(n, c), (lows or {}).get(n, c),
            volume if not callable(volume) else volume(n))
        for n, c in enumerate(closes)
    ]


# ---------------------------------------------------------------- 1-day, YTD

def test_one_day_change():
    change = performance.one_day_change(series([10.0, 11.0]))
    assert (change["value"], change["percent"]) == (1.0, 10.0)
    assert change["from"] == "2026-09-30" and change["to"] == "2026-10-01"
    assert performance.one_day_change(series([10.0])) is None
    assert performance.one_day_change([]) is None


def test_one_day_change_skips_a_missing_day():
    s = series([10.0, 10.5, 12.0])
    del s[1]  # no candle on 30 Sep: compare with 29 Sep, don't invent one
    assert performance.one_day_change(s)["from"] == "2026-09-29"


def test_ytd_is_against_the_last_close_of_last_year():
    s = [Day(date(2025, 12, 30), 8.0, 8, 8, 1), Day(date(2026, 1, 2), 9.0, 9, 9, 1), Day(TODAY, 10.0, 10, 10, 1)]
    ytd = performance.ytd_change(s, TODAY)
    assert (ytd["from"], ytd["value"], ytd["percent"]) == ("2025-12-30", 2.0, 25.0)


def test_ytd_without_history_before_january_is_none():
    s = [Day(date(2026, 3, 1), 9.0, 9, 9, 1), Day(TODAY, 10.0, 10, 10, 1)]
    assert performance.ytd_change(s, TODAY) is None


# ---------------------------------------------------------------- 52-week

def test_52_week_high_and_low_with_dates():
    s = series([10.0] * 400, highs={100: 15.0, 390: 12.0}, lows={20: 5.0, 200: 7.0})
    r = performance.week52_range(s, TODAY)
    # Day 20 (379 days ago) is outside the 52 weeks; day 100 (299 days ago) is in.
    assert (r["high"], r["high_date"]) == (15.0, (TODAY - timedelta(days=299)).isoformat())
    assert (r["low"], r["low_date"]) == (7.0, (TODAY - timedelta(days=199)).isoformat())


def test_52_week_range_counts_only_days_with_trades():
    # A flat no-trade day carries the old price; it isn't a dealt price.
    s = series([10.0, 20.0, 10.0], volume=lambda n: 0 if n == 1 else 100, highs={1: 20.0}, lows={1: 20.0})
    assert performance.week52_range(s, TODAY)["high"] == 10.0


def test_52_week_range_with_no_trades_in_the_period_is_none():
    assert performance.week52_range(series([10.0] * 30, volume=0), TODAY) is None


# ---------------------------------------------------------------- 12-month total return

def test_total_return_adds_dividends_paid_in_the_period():
    s = series([10.0] * 366 + [11.0])  # base 365 days ago: 10.0; now 11.0
    paid = [(TODAY - timedelta(days=100), 0.5), (TODAY - timedelta(days=400), 9.9)]  # the second is too old
    r = performance.total_return_12m(s, TODAY, paid)
    assert r["price_return_percent"] == 10.0
    assert r["dividends"] == 0.5
    assert r["total_return_percent"] == 15.0
    assert len(r["dividends_paid"]) == 1


def test_total_return_needs_a_year_of_history():
    assert performance.total_return_12m(series([10.0] * 100), TODAY, []) is None


def test_dividends_split_across_payment_dates_are_flagged_as_estimated():
    f = FinancialYear(fiscal_year=2025, dividend_per_share=sourced(0.6),
                      dividend_payment_dates=sourced(["2026-04-15", "2026-10-15"]))
    paid, estimated = dividends_paid([f])
    assert [a for _, a in paid] == [0.3, 0.3] and estimated
    usd = FinancialYear(fiscal_year=2025, currency="USD", dividend_per_share=sourced(1.0),
                        dividend_payment_dates=sourced(["2026-04-15"]))
    assert dividends_paid([usd]) == ([], False)  # never added to a GHS price


# ---------------------------------------------------------------- index, beta

def test_simulated_index_is_equal_weighted_from_1000():
    a = series([10.0, 11.0, 11.0])   # +10%, 0%
    b = series([20.0, 20.0, 22.0])   # 0%, +10%
    levels = [d.close for d in simulated_gse_ci({"A": a, "B": b})]
    assert levels == [1000.0, 1050.0, 1102.5]


def test_index_counts_a_day_without_a_candle_as_unchanged():
    a = series([10.0, 11.0])
    b = series([20.0, 22.0])[:1]      # no candle on the second day
    assert [d.close for d in simulated_gse_ci({"A": a, "B": b})] == [1000.0, 1050.0]


def test_beta_is_the_slope_against_the_index():
    import random
    rng = random.Random(1)
    index, stock = [100.0], [50.0]
    for _ in range(60):
        r = rng.gauss(0, 0.01)
        index.append(index[-1] * (1 + r))
        stock.append(stock[-1] * (1 + 2 * r))
    b = performance.beta(series(stock), series(index), TODAY)
    assert b["value"] == pytest.approx(2.0, abs=1e-6) and b["observations"] == 60


def test_beta_needs_enough_days_and_a_moving_index():
    assert performance.beta(series([10.0] * 10), series([1000.0] * 10), TODAY) is None
    flat_index = series([1000.0] * 60)
    assert performance.beta(series([10.0 + n for n in range(60)]), flat_index, TODAY) is None


# ---------------------------------------------------------------- valuation

def _company(**financial):
    return Company(symbol="MTNGH", profile={"shares_outstanding": sourced(1_000_000)},
                   financials=[FinancialYear(fiscal_year=2025, **financial)])


def test_valuation_figures(make_tick):
    company = _company(eps=sourced(0.5), book_value=sourced(2_000_000.0), net_income=sourced(500_000.0),
                       dividend_per_share=sourced(0.25))
    quote = make_tick("MTNGH", vwap=5.0, price=5.0, day_low=4.9, day_high=5.1)
    f = calculated_figures(company, quote, today=TODAY)
    assert f["market_cap"]["value"] == 5_000_000
    assert f["pe_ratio"]["value"] == 10.0
    assert f["price_to_book"]["value"] == 2.5
    assert f["dividend_yield"]["value"] == 5.0
    assert f["return_on_equity"]["value"] == 25.0
    assert all(f[k]["reason"] is None for k in ("market_cap", "pe_ratio", "price_to_book", "return_on_equity"))


def test_negative_earnings_make_pe_na(make_tick):
    quote = make_tick("MTNGH", vwap=5.0, price=5.0, day_low=4.9, day_high=5.1)
    f = calculated_figures(_company(eps=sourced(-0.2)), quote, today=TODAY)
    assert f["pe_ratio"]["value"] is None and f["pe_ratio"]["reason"] == "negative earnings"


def test_missing_eps_and_negative_book_value(make_tick):
    quote = make_tick("MTNGH", vwap=5.0, price=5.0, day_low=4.9, day_high=5.1)
    f = calculated_figures(_company(book_value=sourced(-1.0), net_income=sourced(1.0)), quote, today=TODAY)
    assert f["pe_ratio"]["reason"] == "no eps recorded"
    assert f["price_to_book"]["reason"] == "negative book value"
    assert f["return_on_equity"]["reason"] == "negative book value"
    assert f["market_cap"]["value"] == 5_000_000  # unaffected


def test_trailing_eps_skips_a_year_without_one(make_tick):
    company = Company(symbol="MTNGH", financials=[
        FinancialYear(fiscal_year=2025, revenue=sourced(1.0)),   # results not in yet
        FinancialYear(fiscal_year=2024, eps=sourced(0.25)),
    ])
    quote = make_tick("MTNGH", vwap=5.0, price=5.0, day_low=4.9, day_high=5.1)
    f = calculated_figures(company, quote, today=TODAY)
    assert f["pe_ratio"]["value"] == 20.0 and f["fiscal_years"]["eps"] == 2024


def test_no_quote_falls_back_to_the_latest_close():
    f = calculated_figures(_company(eps=sourced(0.5)), None, daily=series([4.0, 5.0]), today=TODAY)
    assert f["price"]["value"] == 5.0 and "latest daily close" in f["price"]["formula"]
    assert f["pe_ratio"]["value"] == 10.0
    nothing = calculated_figures(None, None, today=TODAY)
    assert nothing["price"]["reason"] == "no price" and nothing["performance"]["one_day"] is None


# ---------------------------------------------------------------- market cap on ticks

async def test_ticks_get_market_cap_from_shares_outstanding(make_tick):
    async def feed():
        yield make_tick("MTNGH", vwap=6.5, price=6.54, day_low=6.4, day_high=6.6)
        yield make_tick("GCB")

    out = [t async for t in with_market_cap(feed(), {"MTNGH": 1_000_000}.get)]
    assert out[0].market_cap == 6_500_000
    assert out[1].market_cap is None  # shares outstanding unknown: no placeholder


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def test_description_has_performance_and_beta_from_the_stored_candles(client):
    calc = client.get("/instruments/MTNGH/description").json()["calculated"]
    perf = calc["performance"]
    assert perf["one_day"]["to"] >= perf["one_day"]["from"]
    assert perf["week52"]["high"] >= perf["week52"]["low"] > 0
    assert perf["ytd"] is not None and perf["total_return_12m"] is not None
    assert perf["total_return_12m"]["dividends_recorded"] is False  # none in the seed yet
    assert calc["beta"]["index"] == "GSE-CI (simulated)" and calc["beta"]["observations"] >= 30
    # Nothing maintained yet, so valuation says why it's empty.
    assert calc["market_cap"]["reason"] == "no shares outstanding recorded"
    assert calc["pe_ratio"]["reason"] == "no eps recorded"


def test_simulated_gse_ci_endpoint(client):
    body = client.get("/indices/gse-ci").json()
    levels = [p["level"] for p in body["series"]]
    assert body["series"][0]["level"] == 1000.0 and len(levels) > 300
    assert all(level > 0 for level in levels)
    assert [p["date"] for p in body["series"]] == sorted(p["date"] for p in body["series"])


def test_quote_cards_get_calculated_market_cap_not_a_placeholder(client, monkeypatch):
    from app import main

    assert main.processor.get_latest("GCB").market_cap is None  # no shares recorded
    shares = Company(symbol="MTNGH", profile={"shares_outstanding": sourced(12_000_000_000)})
    monkeypatch.setitem(main.COMPANIES, "MTNGH", shares)

    def priced():
        q = main.processor.get_latest("MTNGH")
        return q if q.market_cap is not None else None

    deadline = time.monotonic() + 20
    while (quote := priced()) is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert quote is not None, "no MTNGH tick carried a market cap"
    assert quote.market_cap == pytest.approx(quote.vwap * 12_000_000_000)
