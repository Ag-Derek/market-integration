"""Seasonality (app/company/seasonality.py and /seasonality/{symbol}):
month and year returns from month-end closes, per-month statistics over
finished months only, and each year's path from 1 January."""

from datetime import date, timedelta

import pytest

from app.company.performance import Day
from app.company.seasonality import (
    annual_returns, average_path, by_month, monthly_returns, seasonality, year_paths,
)


def day(on: date, close: float) -> Day:
    return Day(on, close, close, close, 100)


def month_ends(*closes_by_month: tuple[int, int, float]) -> list[Day]:
    """A series with one close on the 28th of each (year, month)."""
    return [day(date(y, m, 28), c) for y, m, c in closes_by_month]


TODAY = date(2026, 3, 15)


# ---------------------------------------------------------------- months

def test_month_return_is_last_close_against_the_previous_months():
    series = [
        day(date(2025, 1, 10), 90), day(date(2025, 1, 31), 100),  # January ends at 100
        day(date(2025, 2, 3), 120), day(date(2025, 2, 27), 110),  # February ends at 110
    ]
    months = monthly_returns(series, TODAY)
    assert months[0] == {"year": 2025, "month": 1, "return": None, "partial": False}  # nothing before it
    assert months[1]["return"] == pytest.approx(10.0)


def test_a_gap_leaves_the_month_after_it_without_a_return():
    series = month_ends((2025, 1, 100), (2025, 3, 120), (2025, 4, 132))
    months = {(r["year"], r["month"]): r["return"] for r in monthly_returns(series, TODAY)}
    assert months[(2025, 3)] is None  # February is missing: not a two-month return
    assert months[(2025, 4)] == pytest.approx(10.0)


def test_january_is_measured_from_the_previous_december():
    series = month_ends((2024, 12, 50), (2025, 1, 55))
    assert monthly_returns(series, TODAY)[1]["return"] == pytest.approx(10.0)


def test_the_current_month_is_partial_and_left_out_of_the_averages():
    series = month_ends((2025, 2, 100), (2025, 3, 110), (2026, 2, 100), (2026, 3, 50))
    months = monthly_returns(series, TODAY)
    assert [m["partial"] for m in months] == [False, False, False, True]

    march = by_month(months)[2]
    assert march["count"] == 1 and march["mean"] == pytest.approx(10.0)  # 2026's -50% isn't in it


def test_by_month_statistics():
    series = month_ends(
        (2022, 12, 100), (2023, 1, 110),   # Jan 2023: +10%
        (2023, 12, 100), (2024, 1, 95),    # Jan 2024: -5%
        (2024, 12, 100), (2025, 1, 103),   # Jan 2025: +3%
    )
    jan = by_month(monthly_returns(series, TODAY))[0]
    assert jan["count"] == 3 and jan["positive"] == 2
    assert jan["mean"] == pytest.approx(8 / 3, abs=1e-4)
    assert jan["median"] == pytest.approx(3.0)
    assert jan["best"] == {"year": 2023, "return": pytest.approx(10.0)}
    assert jan["worst"] == {"year": 2024, "return": pytest.approx(-5.0)}


def test_every_month_is_listed_even_without_data():
    stats = by_month([])
    assert [s["month"] for s in stats] == list(range(1, 13))
    assert stats[5] == {"month": 6, "count": 0, "positive": 0, "mean": None, "median": None,
                        "best": None, "worst": None}


# ---------------------------------------------------------------- years

def test_annual_return_from_the_previous_december():
    series = month_ends((2024, 6, 80), (2024, 12, 100), (2025, 6, 90), (2025, 12, 120), (2026, 2, 60))
    years = {r["year"]: r for r in annual_returns(series, TODAY)}
    assert years[2024]["return"] is None
    assert years[2025]["return"] == pytest.approx(20.0)
    assert years[2026] == {"year": 2026, "return": pytest.approx(-50.0), "partial": True}


def test_year_paths_start_from_the_previous_years_close():
    series = [day(date(2024, 12, 31), 100), day(date(2025, 1, 2), 105), day(date(2025, 2, 1), 90)]
    paths = {p["year"]: p for p in year_paths(series)}

    assert paths[2025]["partial_start"] is False
    assert paths[2025]["points"] == [[2, pytest.approx(5.0)], [32, pytest.approx(-10.0)]]
    # 2024 has no year before it: measured from its own first close.
    assert paths[2024] == {"year": 2024, "partial_start": True, "points": [[366, 0.0]]}


def test_average_path_uses_finished_full_years_and_carries_values_forward():
    paths = [
        {"year": 2023, "partial_start": True, "points": [[1, 50.0]]},     # no base: left out
        {"year": 2024, "partial_start": False, "points": [[2, 10.0], [365, 20.0]]},
        {"year": 2025, "partial_start": False, "points": [[3, -10.0], [365, 0.0]]},
        {"year": 2026, "partial_start": False, "points": [[2, 99.0]]},    # this year: left out
    ]
    points, n = average_path(paths, TODAY)
    assert n == 2
    avg = dict((d, v) for d, v in points)
    assert avg[1] == 0.0                         # neither has a day 1 yet
    assert avg[2] == pytest.approx(5.0)          # (10 + 0) / 2
    assert avg[200] == pytest.approx(0.0)        # (10 + -10) / 2, carried forward
    assert avg[365] == pytest.approx(10.0)
    assert len(points) == 365


def test_seasonality_of_an_empty_series():
    out = seasonality([], TODAY)
    assert out["from"] is None and out["months"] == [] and out["paths"] == []
    assert out["average"] == [] and out["average_years"] == 0
    assert len(out["by_month"]) == 12


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def test_seasonality_api_covers_every_stored_year(client):
    body = client.get("/seasonality/mtngh").json()

    assert body["symbol"] == "MTNGH" and body["currency"] == "GHS"
    years = sorted({m["year"] for m in body["months"]})
    # The mock invents 10 years of daily history; all of it is kept.
    assert len(years) >= 10
    assert body["average_years"] >= 8
    assert all(s["count"] >= 8 for s in body["by_month"])
    assert sum(m["partial"] for m in body["months"]) == 1
    assert body["paths"][-1]["year"] == date.today().year


def test_the_stock_page_has_a_seasonality_view(client):
    script = client.get("/static/seasonality.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "window.Seasonality = { mount: mount }" in script.text

    page = client.get("/stock/MTNGH").text
    assert '<script src="/static/seasonality.js"></script>' in page
    assert page.index("/static/seasonality.js") < page.index("<script>")
    assert 'href="#seasonality"' in page and 'id="seasonality"' in page


def test_seasonality_api_is_for_equities_only(client):
    assert client.get("/seasonality/NOPE").status_code == 404
    assert client.get("/seasonality/GHGGOG069931").status_code == 404
    # A suspended equity has a page but no candles: empty, not an error.
    suspended = client.get("/seasonality/ALW").json()
    assert suspended["months"] == [] and suspended["average_years"] == 0
