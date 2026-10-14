"""The stock chart's technical indicators (/static/indicators.js), run
under Node against independent references: RSI against StockCharts'
worked example of Wilder's method, the rest against straight Python
transcriptions of their textbook definitions. Plus /candles?warmup=,
which feeds them the history ahead of a chart's first candle."""

import json
import math
import random
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "app" / "static" / "indicators.js"

node = shutil.which("node")
needs_node = pytest.mark.skipif(node is None, reason="Node.js isn't installed")


def run_js(calls: dict) -> dict:
    """{name: [function, *args]} -> {name: result}, evaluated by indicators.js."""
    program = (
        f"const I = require({json.dumps(str(SCRIPT))});"
        f"const calls = {json.dumps(calls)};"
        "const out = {};"
        "for (const k in calls) out[k] = I[calls[k][0]].apply(null, calls[k].slice(1));"
        "process.stdout.write(JSON.stringify(out));"
    )
    done = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True)
    return json.loads(done.stdout)


def close_to(actual, expected, tol=1e-9):
    assert len(actual) == len(expected)
    for a, e in zip(actual, expected):
        if e is None:
            assert a is None
        else:
            assert a == pytest.approx(e, abs=tol)


# ---------------------------------------------------------------- references

def ref_sma(xs, n):
    return [None if i < n - 1 else sum(xs[i - n + 1:i + 1]) / n for i in range(len(xs))]


def ref_ema(xs, n):
    out = [None] * len(xs)
    first = next((i for i, x in enumerate(xs) if x is not None), len(xs))
    if len(xs) - first < n:
        return out
    prev = sum(xs[first:first + n]) / n
    out[first + n - 1] = prev
    for i in range(first + n, len(xs)):
        prev = (2 / (n + 1)) * xs[i] + (1 - 2 / (n + 1)) * prev
        out[i] = prev
    return out


def walk(n, seed=7):
    rng = random.Random(seed)
    xs, p = [], 20.0
    for _ in range(n):
        p *= math.exp(rng.gauss(0, 0.02))
        xs.append(round(p, 2))
    return xs


# StockCharts' RSI example (14 periods): closes, then the first RSIs.
STOCKCHARTS_CLOSES = [
    44.3389, 44.0902, 44.1497, 43.6124, 44.2779, 44.8264, 45.0955, 45.4245, 45.8433, 46.0826,
    45.8931, 46.0328, 45.6140, 46.2820, 46.2820, 46.0028, 46.0328, 46.4116, 46.2222, 45.6439,
    46.2122, 46.2521, 45.7137, 46.4515, 45.7835, 45.3548, 44.0288, 44.1783, 44.2181, 44.5672,
    43.4205, 42.6628, 43.1314,
]
STOCKCHARTS_RSI = [
    70.53, 66.32, 66.55, 69.41, 66.36, 57.97, 62.93, 63.26, 56.06, 62.38,
    54.71, 50.42, 39.99, 41.46, 41.87, 45.46, 37.30, 33.08, 37.77,
]


# ---------------------------------------------------------------- math

@needs_node
def test_rsi_matches_wilders_method():
    out = run_js({"rsi": ["rsi", STOCKCHARTS_CLOSES, 14]})["rsi"]
    assert out[:14] == [None] * 14
    assert out[14:] == pytest.approx(STOCKCHARTS_RSI, abs=0.01)


@needs_node
def test_rsi_of_a_one_way_or_flat_series():
    out = run_js({
        "up": ["rsi", [1, 2, 3, 4, 5, 6], 3],
        "flat": ["rsi", [5, 5, 5, 5, 5], 3],
    })
    assert out["up"][3:] == [100, 100, 100]
    assert out["flat"][3:] == [50, 50]


@needs_node
def test_moving_averages_bands_and_macd_match_their_definitions():
    xs = walk(120)
    out = run_js({
        "sma": ["sma", xs, 20],
        "ema": ["ema", xs, 20],
        "bb": ["bollinger", xs, 20, 2],
        "macd": ["macd", xs, 12, 26, 9],
    })

    close_to(out["sma"], ref_sma(xs, 20))
    close_to(out["ema"], ref_ema(xs, 20))

    mid = ref_sma(xs, 20)
    sd = [None if m is None else math.sqrt(sum((x - m) ** 2 for x in xs[i - 19:i + 1]) / 20)
          for i, m in enumerate(mid)]
    close_to(out["bb"]["mid"], mid)
    close_to(out["bb"]["upper"], [None if m is None else m + 2 * s for m, s in zip(mid, sd)])
    close_to(out["bb"]["lower"], [None if m is None else m - 2 * s for m, s in zip(mid, sd)])

    fast, slow = ref_ema(xs, 12), ref_ema(xs, 26)
    line = [None if s is None else f - s for f, s in zip(fast, slow)]
    signal = ref_ema(line, 9)
    close_to(out["macd"]["macd"], line)
    close_to(out["macd"]["signal"], signal)
    close_to(out["macd"]["hist"], [None if g is None else m - g for m, g in zip(line, signal)])
    # The signal line starts once 9 MACD values exist: index 25 + 8.
    assert out["macd"]["signal"][32] is None and out["macd"]["signal"][33] is not None


@needs_node
def test_too_short_a_series_has_no_values():
    out = run_js({"sma": ["sma", [1, 2], 5], "ema": ["ema", [1, 2], 5], "rsi": ["rsi", [1, 2], 5]})
    assert out == {"sma": [None, None], "ema": [None, None], "rsi": [None, None]}


# ---------------------------------------------------------------- page

def test_the_stock_page_loads_the_indicators(client):
    script = client.get("/static/indicators.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]

    page = client.get("/stock/MTNGH").text
    assert '<script src="/static/indicators.js"></script>' in page
    assert page.index("/static/indicators.js") < page.index("<script>")
    assert 'id="indMenu"' in page
    # Fetches the warm-up with the range, matching the server's cap.
    assert "const MAX_PERIOD = 200;" in page and '"&warmup=" + MAX_PERIOD' in page


# ---------------------------------------------------------------- warmup
# `client` is the session-wide running app from conftest.py.

def test_candles_warmup_returns_history_ahead_of_the_range(client):
    plain = client.get("/candles", params={"symbol": "MTNGH", "range": "1Y"}).json()
    body = client.get("/candles", params={"symbol": "MTNGH", "range": "1Y", "warmup": 200}).json()

    assert plain["warmup"] == []
    warmup = body["warmup"]
    assert len(warmup) == 200
    times = [c["time"] for c in warmup]
    assert times == sorted(times)
    # It ends right where the range begins, and the range itself is unchanged.
    assert times[-1] < body["candles"][0]["time"]
    assert [c["time"] for c in body["candles"]] == [c["time"] for c in plain["candles"]]


@pytest.mark.parametrize("range_", ["1D", "5D", "1M"])
def test_intraday_ranges_have_room_for_a_long_indicator(client, range_):
    body = client.get("/candles", params={"symbol": "MTNGH", "range": range_, "warmup": 200}).json()
    assert len(body["warmup"]) == 200


def test_max_has_no_warmup_and_warmup_is_bounded(client):
    body = client.get("/candles", params={"symbol": "MTNGH", "range": "Max", "warmup": 50}).json()
    assert body["warmup"] == []
    assert client.get("/candles", params={"symbol": "MTNGH", "warmup": 201}).status_code == 422
    assert client.get("/candles", params={"symbol": "MTNGH", "warmup": -1}).status_code == 422
