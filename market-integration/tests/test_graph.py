"""The /graph page's maths (app/analytics/graph.py) and API: series
rebased to 100 at the range's start, and the correlation matrix of their
changes, measured between days both series traded."""

import math
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from app.analytics.graph import (
    GRAPH_RANGES,
    MIN_OBSERVATIONS,
    Point,
    correlation_matrix,
    observations,
    paired_changes,
    pearson,
    rebase,
    weekly,
)
from tests.test_fx import USD_MID, fix, weekdays

START = date(2026, 1, 1)


def daily(values, start=START, observed=None):
    return [
        Point(start + timedelta(days=i), v, True if observed is None else observed[i])
        for i, v in enumerate(values)
    ]


# ---------------------------------------------------------------- rebasing

def test_rebase_divides_by_the_value_at_the_start():
    points = daily([50, 40, 44, 60])
    out = rebase(points, START + timedelta(days=1))
    assert out["base"] == {"date": "2026-01-02", "value": 40}
    assert out["late_start"] is False
    assert [p[2] for p in out["points"]] == [100.0, 110.0, 150.0]


def test_a_start_on_a_day_without_a_value_rebases_on_the_last_one_before():
    # Friday and Monday only: a range starting Sunday is based on Friday.
    points = [Point(date(2026, 1, 2), 10.0), Point(date(2026, 1, 5), 12.0)]
    out = rebase(points, date(2026, 1, 4))
    assert out["base"]["date"] == "2026-01-02"
    assert out["points"] == [["2026-01-02", 10.0, 100.0], ["2026-01-05", 12.0, 120.0]]


def test_a_series_that_starts_inside_the_range_is_based_on_its_first_value():
    out = rebase(daily([8, 10], start=date(2026, 3, 1)), START)
    assert out["base"]["date"] == "2026-03-01" and out["late_start"] is True
    assert out["points"][-1][2] == pytest.approx(125.0)


def test_max_rebases_on_the_first_value_without_calling_it_late():
    out = rebase(daily([4, 5]), None)
    assert out["late_start"] is False and out["points"][-1][2] == pytest.approx(125.0)


def test_rebase_of_nothing_and_of_a_zero_base():
    assert rebase([], START) is None
    assert [p[2] for p in rebase(daily([0, 5]), None)["points"]] == [None, None]


def test_weekly_keeps_the_base_and_each_weeks_last_point():
    # 2026-01-01 is a Thursday: Thu-Sun is one ISO week, Mon-Sun the next.
    rows = [[(START + timedelta(days=i)).isoformat(), i, i] for i in range(12)]
    kept = [r[0] for r in weekly(rows)]
    assert kept == ["2026-01-01", "2026-01-04", "2026-01-11", "2026-01-12"]


def test_graph_ranges():
    today = date(2026, 10, 14)
    assert GRAPH_RANGES["1Y"].start(today) == date(2025, 10, 14)
    assert GRAPH_RANGES["YTD"].start(today) == date(2026, 1, 1)
    assert GRAPH_RANGES["Max"].start(today) is None
    assert GRAPH_RANGES["5Y"].weekly and not GRAPH_RANGES["2Y"].weekly


# ---------------------------------------------------------------- correlation

def test_pearson():
    xs = [i * 0.01 for i in range(30)]
    assert pearson([(x, 2 * x + 1) for x in xs]) == pytest.approx(1.0)
    assert pearson([(x, -x) for x in xs]) == pytest.approx(-1.0)
    assert pearson([(x, 1.0) for x in xs]) is None                 # one side never moved
    assert pearson([(x, x) for x in xs[:MIN_OBSERVATIONS - 1]]) is None


def test_changes_are_measured_between_days_both_traded():
    # A trades on days 0, 3 and 4; B every day.
    a = observations(daily([10, 10, 10, 11, 12.1], observed=[True, False, False, True, True]), None)
    b = observations(daily([100, 101, 103, 105, 99]), None)
    pairs = paired_changes(a, "return", b, "return")
    assert pairs == [
        (pytest.approx(0.10), pytest.approx(0.05)),        # day 0 -> 3, for both
        (pytest.approx(0.10), pytest.approx(99 / 105 - 1)),  # day 3 -> 4
    ]


def test_yields_change_by_difference():
    a = observations(daily([20.0, 20.5]), None)
    b = observations(daily([10.0, 11.0]), None)
    assert paired_changes(a, "difference", b, "return") == [(pytest.approx(0.5), pytest.approx(0.1))]


def test_weekly_observations_are_each_weeks_last():
    obs = observations(daily([1, 2, 3, 4, 5], observed=[True, True, True, False, True]), None, "weekly")
    # Thu 1 Jan - Sun 4 Jan: the last observed is Sat 3 Jan (3); Mon 5 Jan starts week 2.
    assert obs == {(2026, 1): 3, (2026, 2): 5}


def test_observations_start_at_the_range():
    obs = observations(daily([1, 2, 3]), START + timedelta(days=1))
    assert list(obs.values()) == [2, 3]


def thin_and_liquid(days=400, trade_every=4, seed=7):
    """A liquid stock and a thin one driven by the same daily moves:
    the thin one only prints every `trade_every` days, carrying its last
    close in between."""
    rng = random.Random(seed)
    moves = [rng.gauss(0, 0.02) for _ in range(days)]
    liquid, thin, observed = [100.0], [100.0], [True]
    level = 100.0
    for i, r in enumerate(moves[1:], start=1):
        level *= math.exp(r)
        liquid.append(level)
        trades = i % trade_every == 0
        thin.append(level if trades else thin[-1])
        observed.append(trades)
    return daily(liquid), daily(thin, observed=observed)


def test_thin_trading_does_not_dilute_the_correlation():
    liquid, thin = thin_and_liquid()
    matrix = correlation_matrix([(liquid, "return"), (thin, "return")], None)
    cell = matrix[0][1]
    # Same underlying moves: measured between days both traded, they
    # are perfectly correlated, on a quarter of the days.
    assert cell["value"] == pytest.approx(1.0, abs=1e-6)
    assert cell["observations"] == 399 // 4
    assert matrix[1][0] == cell

    # Calendar-day returns on the carried closes would say far less.
    naive = [
        (liquid[i].value / liquid[i - 1].value - 1, thin[i].value / thin[i - 1].value - 1)
        for i in range(1, len(liquid))
    ]
    assert pearson(naive) < 0.6


def test_the_matrix_diagonal_and_too_few_observations():
    liquid, _ = thin_and_liquid()
    rare = daily([1.0 + i for i in range(400)], observed=[i % 40 == 0 for i in range(400)])  # 10 trades
    flat = daily([5.0] * 400)
    matrix = correlation_matrix([(liquid, "return"), (rare, "return"), (flat, "return")], None)
    assert matrix[0][0] == {"value": 1.0, "observations": 399}
    assert matrix[1][1] == {"value": None, "observations": 9}
    assert matrix[0][1] == {"value": None, "observations": 9}
    assert matrix[2][2]["value"] is None          # never moved
    assert matrix[0][2]["value"] is None


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

@pytest.fixture(scope="module")
def usd_rates(client):
    """The same USD fixes test_fx stores, so the two modules agree."""
    from app.main import fx_rates

    today = datetime.now(timezone.utc).date()
    days = list(weekdays(today - timedelta(days=6 * 365), today))
    fx_rates._store.upsert([fix(d, mid=USD_MID(d)) for d in days])
    fx_rates.reload()
    return fx_rates


def test_graph_series_rebases_every_kind_of_series(client, usd_rates):
    bond = "GHGGOG069931"
    body = client.get(f"/graph/series?series=mtngh,GSE-CI,{bond},fx:usd&range=1Y").json()
    today = datetime.now(timezone.utc).date()
    assert body["range"] == "1Y" and body["step"] == "1d"
    assert body["start"] == (today - timedelta(days=365)).isoformat()

    by_id = {s["id"]: s for s in body["series"]}
    assert list(by_id) == ["MTNGH", "GSE-CI", bond, "FX:USD"]
    assert [by_id[i]["kind"] for i in by_id] == ["equity", "index", "yield", "fx"]
    assert by_id[bond]["change"] == "difference" and by_id["MTNGH"]["change"] == "return"
    assert by_id[bond]["label"] == "GOG 8.65% Feb 2029"
    assert by_id["FX:USD"]["label"] == "USD/GHS"

    for s in body["series"]:
        first = s["points"][0]
        assert first[0] == s["base"]["date"] <= body["start"]  # based on or before the start
        assert first[2] == pytest.approx(100.0)
        assert s["last"]["rebased"] == pytest.approx(s["last"]["value"] / s["base"]["value"] * 100, rel=1e-4)
        assert 0 < s["observed_days"] <= s["days"]

    usd = by_id["FX:USD"]
    assert usd["last"]["value"] == pytest.approx(USD_MID(date.fromisoformat(usd["last"]["date"])))


def test_long_ranges_draw_one_point_a_week(client):
    body = client.get("/graph/series?series=MTNGH&range=5Y").json()
    s = body["series"][0]
    assert body["step"] == "1w"
    assert 255 <= len(s["points"]) <= 263
    assert s["days"] > 5 * 365  # thinned for drawing only


def test_graph_correlation(client):
    body = client.get("/graph/correlation?series=MTNGH,GCB,GSE-CI&range=1Y").json()
    assert body["frequency"] == "daily" and body["min_observations"] == MIN_OBSERVATIONS
    assert [s["id"] for s in body["series"]] == ["MTNGH", "GCB", "GSE-CI"]
    m = body["matrix"]
    for i in range(3):
        assert m[i][i]["value"] == 1.0
        for j in range(3):
            assert m[i][j] == m[j][i]
            assert m[i][j]["value"] is None or -1 <= m[i][j]["value"] <= 1
    assert m[0][1]["observations"] >= MIN_OBSERVATIONS

    weekly_body = client.get("/graph/correlation?series=MTNGH,GCB&range=1Y&frequency=weekly").json()
    assert 50 <= weekly_body["matrix"][0][1]["observations"] <= 53


@pytest.mark.parametrize("query, status", [
    ("series=NOPE", 404),
    ("series=MTNGH&range=1D", 400),
    ("series=" + ",".join(["MTNGH", "GCB", "SCB", "CAL", "EGH", "TOTAL", "GOIL", "FML", "SIC"]), 400),
    ("series=FX:XYZ", 400),
    ("series=FX:GHS", 400),
    ("series=,", 400),
])
def test_graph_errors(client, query, status):
    assert client.get("/graph/series?" + query).status_code == status


def test_the_graph_page_is_served_and_linked(client):
    page = client.get("/graph")
    assert page.status_code == 200
    assert 'id="chart"' in page.text and 'id="correlation"' in page.text
    assert "/graph/series?" in page.text and "/graph/correlation?" in page.text
    for path in ("/ticker", "/fixed-income", "/yield-curve", "/tickets"):
        assert '<a class="tab" href="/graph">Graph</a>' in client.get(path).text
