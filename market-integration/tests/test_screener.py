"""The stock screener (#99): filters, null handling, sort order, and the
/screener/results endpoint over every company file. docs/screener.md."""

from datetime import date

import pytest

from app.company import COMPANIES
from app.company.figures import calculated_figures
from app.company.performance import Day
from app.models.company import Company, FinancialYear
from app.models.screener import ScreenerSort
from app.screener import COLUMNS, FILTERS, Entry, screen

TODAY = date(2026, 10, 1)
SRC = {"source": "Test fixture", "as_of": "2025-12-31"}


def sourced(value):
    return {"value": value, **SRC}


def entry(symbol, sector, price=None, shares=None, currency="GHS", **financial):
    """A company priced at its latest daily close (no live quote), with
    one fiscal year of `financial` figures."""
    profile = {"shares_outstanding": sourced(shares)} if shares else {}
    financials = [FinancialYear(fiscal_year=2025, currency=currency,
                                **{k: sourced(v) for k, v in financial.items()})] if financial else []
    company = Company(symbol=symbol, profile=profile, financials=financials)
    daily = [Day(TODAY, price, price, price, 100)] if price is not None else []
    return Entry(symbol=symbol, name=f"{symbol} PLC", sector=sector,
                 figures=calculated_figures(company, None, daily=daily, today=TODAY))


@pytest.fixture
def universe():
    return [
        # P/E 10, P/B 2, yield 5%, ROE 20%, cap 100m
        entry("AAA", "Banking", price=10.0, shares=10_000_000, eps=1.0, dividend_per_share=0.5,
              book_value=50_000_000.0, net_income=10_000_000.0),
        # loss-maker: no P/E; P/B 0.5, yield 5%, ROE -2.5%, cap 4m
        entry("BBB", "Banking", price=4.0, shares=1_000_000, eps=-0.2, dividend_per_share=0.2,
              book_value=8_000_000.0, net_income=-200_000.0),
        # no dividend recorded: P/E 5, P/B 2, ROE 40%, cap 40m
        entry("CCC", "Energy", price=20.0, shares=2_000_000, eps=4.0,
              book_value=20_000_000.0, net_income=8_000_000.0),
        # no price at all (suspended, never traded)
        entry("DDD", "Energy", shares=1_000_000, eps=1.0, dividend_per_share=0.1,
              book_value=1_000_000.0, net_income=100_000.0),
        # reports in USD: no ratios taken across currencies
        entry("EEE", "Mining", price=3.0, shares=1_000_000, currency="USD", eps=0.5,
              dividend_per_share=0.1, book_value=1_000_000.0, net_income=200_000.0),
    ]


def symbols(result):
    return [r.symbol for r in result.rows]


def only_filter(result):
    (f,) = result.filters
    return f


# ---------------------------------------------------------------- no filters

def test_with_no_filters_every_company_appears(universe):
    result = screen(universe)
    assert result.universe == result.count == 5
    assert set(symbols(result)) == {"AAA", "BBB", "CCC", "DDD", "EEE"}
    assert result.filters == []
    assert result.sectors == ["Banking", "Energy", "Mining"]


def test_every_figure_and_column_carries_its_formula(universe):
    result = screen(universe)
    assert [c.key for c in result.columns] == [key for key, *_ in COLUMNS]
    assert all(c.formula for c in result.columns)
    pe = next(c for c in result.columns if c.key == "pe_ratio")
    assert pe.formula == "price / trailing EPS" and pe.unit == "x"
    for row in result.rows:
        for key, *_ in COLUMNS:
            assert getattr(row, key).formula


# ---------------------------------------------------------------- each filter

def test_pe_max_is_inclusive_and_leaves_out_companies_without_a_pe(universe):
    result = screen(universe, pe_max=10)
    assert symbols(result) == ["AAA", "CCC"]  # 10 is kept: bounds are inclusive
    f = only_filter(result)
    assert (f.param, f.figure, f.bound, f.threshold) == ("pe_max", "pe_ratio", "max", 10)
    assert f.left_out == 3 and f.symbols == ["BBB", "DDD", "EEE"]
    assert f.reasons == {"eps only reported in another currency": 1, "negative earnings": 1, "no price": 1}
    assert f.note.startswith("3 companies were left out because they have no P/E: ")


def test_a_single_reason_reads_as_a_sentence(universe):
    f = only_filter(screen(universe, sector="Banking", pe_max=100))
    assert f.left_out == 1 and f.symbols == ["BBB"]
    assert f.note == "1 company was left out because it has negative earnings."


def test_yield_min(universe):
    result = screen(universe, yield_min=5)
    assert set(symbols(result)) == {"AAA", "BBB"}  # 5% is kept: bounds are inclusive
    f = only_filter(result)
    assert f.left_out == 3 and f.symbols == ["CCC", "DDD", "EEE"]
    assert f.reasons["no dividend per share recorded"] == 1


def test_pb_max(universe):
    result = screen(universe, pb_max=1)
    assert symbols(result) == ["BBB"]
    assert only_filter(result).symbols == ["DDD", "EEE"]


def test_roe_min(universe):
    # ROE needs no price, and net income / book value is within one
    # currency, so DDD (unpriced) and EEE (USD) both have one.
    result = screen(universe, roe_min=20)
    assert set(symbols(result)) == {"AAA", "CCC", "EEE"}
    assert only_filter(result).left_out == 0
    assert only_filter(result).note is None


def test_market_cap_min(universe):
    result = screen(universe, market_cap_min=40_000_000)
    assert set(symbols(result)) == {"AAA", "CCC"}
    f = only_filter(result)
    assert f.symbols == ["DDD"] and f.reasons == {"no price": 1}


def test_sector_is_case_insensitive_and_narrows_the_universe(universe):
    result = screen(universe, sector="  energy ")
    assert result.sector == "Energy" and result.universe == 2
    assert set(symbols(result)) == {"CCC", "DDD"}
    assert result.sectors == ["Banking", "Energy", "Mining"]  # the menu still lists every sector


def test_unknown_sector_or_filter_is_an_error(universe):
    with pytest.raises(ValueError, match="no company in sector"):
        screen(universe, sector="Shipping")
    with pytest.raises(ValueError, match="unknown filter"):
        screen(universe, eps_min=1)


def test_filters_combine_and_each_counts_its_own_left_outs(universe):
    result = screen(universe, pe_max=10, yield_min=5)
    assert symbols(result) == ["AAA"]  # CCC has no yield, BBB no P/E
    by_param = {f.param: f for f in result.filters}
    assert by_param["pe_max"].symbols == ["BBB", "DDD", "EEE"]
    assert by_param["yield_min"].symbols == ["CCC", "DDD", "EEE"]  # DDD, EEE counted under both


# ---------------------------------------------------------------- nulls are never zero

def test_a_missing_figure_is_not_zero_but_a_recorded_zero_is():
    no_dividend = entry("NOD", "Banking", price=5.0, shares=1, eps=1.0)
    zero_dividend = entry("ZER", "Banking", price=5.0, shares=1, eps=1.0, dividend_per_share=0.0)
    result = screen([no_dividend, zero_dividend], yield_min=0)
    assert symbols(result) == ["ZER"]
    assert only_filter(result).symbols == ["NOD"]
    # A loss-maker isn't a P/E of 0 under any ceiling.
    loss = entry("LOS", "Banking", price=5.0, shares=1, eps=-1.0)
    assert symbols(screen([loss], pe_max=1_000_000)) == []


# ---------------------------------------------------------------- sort order

def test_default_sort_is_highest_yield_first_with_nulls_last(universe):
    result = screen(universe)
    assert result.sort == ScreenerSort.dividend_yield
    assert symbols(result) == ["AAA", "BBB", "CCC", "DDD", "EEE"]  # 5%, 5% (tie by symbol), then no yield


def test_sort_by_lowest_pe(universe):
    assert symbols(screen(universe, sort=ScreenerSort.pe_ratio)) == ["CCC", "AAA", "BBB", "DDD", "EEE"]


def test_sort_by_highest_roe_and_largest_market_cap(universe):
    assert symbols(screen(universe, sort=ScreenerSort.return_on_equity)) == ["CCC", "AAA", "EEE", "DDD", "BBB"]
    assert symbols(screen(universe, sort=ScreenerSort.market_cap)) == ["AAA", "CCC", "BBB", "EEE", "DDD"]


def test_sort_by_lowest_pb_and_symbol(universe):
    assert symbols(screen(universe, sort=ScreenerSort.price_to_book)) == ["BBB", "AAA", "CCC", "DDD", "EEE"]
    assert symbols(screen(list(reversed(universe)), sort=ScreenerSort.symbol)) == ["AAA", "BBB", "CCC", "DDD", "EEE"]


def test_sorting_never_drops_a_row(universe):
    for sort in ScreenerSort:
        assert screen(universe, sort=sort).count == 5


# ---------------------------------------------------------------- the endpoint

def test_every_company_file_appears_with_no_filters(client):
    result = client.get("/screener/results").json()
    assert {r["symbol"] for r in result["rows"]} == set(COMPANIES)
    assert result["universe"] == result["count"] == len(COMPANIES)
    assert result["filters"] == []


@pytest.mark.parametrize("param", sorted(FILTERS))
def test_the_left_out_count_is_right_for_each_filter(client, param):
    figure, bound, _ = FILTERS[param]
    everyone = client.get("/screener/results").json()["rows"]
    missing = sorted(r["symbol"] for r in everyone if r[figure]["value"] is None)
    # A bound every value passes: only the missing are left out.
    lenient = -1e18 if bound == "min" else 1e18
    result = client.get("/screener/results", params={param: lenient}).json()
    (f,) = result["filters"]
    assert f["left_out"] == len(missing) and f["symbols"] == missing
    assert sum(f["reasons"].values()) == f["left_out"]
    assert result["count"] + f["left_out"] == result["universe"]


def test_endpoint_sector_and_sort(client):
    result = client.get("/screener/results", params={"sector": "banking", "sort": "pe_ratio"}).json()
    assert result["sector"] == "Banking" and result["rows"]
    assert all(r["sector"] == "Banking" for r in result["rows"])
    pes = [r["pe_ratio"]["value"] for r in result["rows"]]
    known = [v for v in pes if v is not None]
    assert known == sorted(known) and pes == known + [None] * (len(pes) - len(known))


def test_endpoint_rejects_an_unknown_sector_or_sort(client):
    bad = client.get("/screener/results", params={"sector": "Shipping"})
    assert bad.status_code == 400 and "no company in sector" in bad.json()["detail"]
    assert client.get("/screener/results", params={"sort": "dcf_upside"}).status_code == 422


def test_the_screener_page_is_served_and_shows_formulas_on_hover(client):
    page = client.get("/screener")
    assert page.status_code == 200 and "<title>Stock Screener</title>" in page.text
    assert 'href="/screener" aria-current="page">Screener</a>' in page.text
    assert 'fetch("/screener/results"' in page.text
    assert "th.title = c.formula" in page.text  # column headings
    assert "td.title = figure.formula" in page.text  # each cell, and an n/a's reason
    assert "MarketSearch.mount(" in page.text
