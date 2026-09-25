import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.aggregation.market_aggregator import MarketAggregator
from app.aggregation.ranges import RANGES
from app.connectors.market_connector import MockMarketConnector
from app.models.candle import Candle, bucket_start, resample
from app.validation.market_validator import validate_candle


async def _no_feed():
    return
    yield  # pragma: no cover - makes this an async generator


def _candle(symbol, interval, start, o, h, l, c, v, tick_count=0):
    return Candle(
        symbol=symbol, interval=interval, window_start=start,
        window_end=start + timedelta(minutes=5), open=o, high=h, low=l, close=c,
        volume=v, tick_count=tick_count,
    )


# ---------------------------------------------------------------- time grid

def test_bucket_start_aligns_to_the_interval_grid():
    ts = datetime(2026, 9, 23, 10, 7, 42, tzinfo=timezone.utc)  # a Wednesday

    assert bucket_start(ts, "5m") == datetime(2026, 9, 23, 10, 5, tzinfo=timezone.utc)
    assert bucket_start(ts, "15m") == datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert bucket_start(ts, "1h") == datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert bucket_start(ts, "1d") == datetime(2026, 9, 23, tzinfo=timezone.utc)
    # Weeks start on Monday, not on the epoch's Thursday.
    assert bucket_start(ts, "1w") == datetime(2026, 9, 21, tzinfo=timezone.utc)


def test_resample_rolls_fine_candles_into_coarser_ones():
    t0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    fine = [
        _candle("AAPL", "5m", t0, 10, 12, 9, 11, 100),
        _candle("AAPL", "5m", t0 + timedelta(minutes=5), 11, 15, 10, 14, 50),
        _candle("AAPL", "5m", t0 + timedelta(minutes=10), 14, 14, 8, 9, 25),
        _candle("AAPL", "5m", t0 + timedelta(minutes=15), 9, 10, 9, 10, 5),
    ]

    coarse = resample(fine, "15m")

    assert len(coarse) == 2
    first = coarse[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (10, 15, 8, 9, 175)
    assert first.window_end == t0 + timedelta(minutes=15)
    assert coarse[1].window_start == t0 + timedelta(minutes=15)


# ---------------------------------------------------------------- validation

def test_validate_candle_accepts_a_well_formed_candle():
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    assert validate_candle(_candle("NVDA", "5m", t0, 483.40, 484.19, 482.71, 483.21, 136_192))


@pytest.mark.parametrize("o,h,l,c,v,tick_count,expected", [
    (10, 12, 9, 13, 1, 0, "close (13.0) outside"),
    (8, 12, 9, 11, 1, 0, "open (8.0) outside"),
    (10, 9, 12, 11, 1, 0, "low (12.0) > high (9.0)"),
    (10, 12, 0, 11, 1, 0, "prices must be positive"),
    (10, 12, 9, 11, -1, 0, "volume (-1) is negative"),
    (10, 12, 9, 11, 1, -1, "tick_count (-1) is negative"),
])
def test_validate_candle_rejects_broken_ohlcv(o, h, l, c, v, tick_count, expected):
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    result = validate_candle(_candle("NVDA", "5m", t0, o, h, l, c, v, tick_count))
    assert not result
    assert any(expected in e for e in result.errors)


def test_validate_candle_rejects_windows_off_the_grid():
    t0 = datetime(2026, 9, 18, 9, 31, tzinfo=timezone.utc)
    result = validate_candle(_candle("NVDA", "5m", t0, 10, 12, 9, 11, 1))
    assert not result
    assert any("not aligned" in e for e in result.errors)

    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    wrong_end = _candle("NVDA", "15m", t0, 10, 12, 9, 11, 1)  # helper sets a 5m window_end
    assert any("window_end" in e for e in validate_candle(wrong_end).errors)


async def test_aggregator_skips_invalid_candles_when_flushing(tmp_path):
    db_path = tmp_path / "c.db"
    aggregator = MarketAggregator(_no_feed(), db_path=db_path, intervals=("5m",))
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    aggregator._pending = [
        _candle("NVDA", "5m", t0, 10, 12, 9, 11, 1, tick_count=3),
        _candle("NVDA", "5m", t0 + timedelta(minutes=5), 11, 10, 12, 11, 1, tick_count=3),
    ]

    await aggregator._flush()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT window_start FROM market_candles").fetchall()
    finally:
        conn.close()
    assert rows == [(t0.isoformat(),)]


# ---------------------------------------------------------------- aggregator

def test_live_candles_count_every_tick(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("15m",))
    t0 = datetime(2026, 9, 18, 9, 30, tzinfo=timezone.utc)
    for i in range(4):
        aggregator._record(make_tick(price=100.0 + i, volume=1_000 + i, timestamp=t0 + timedelta(minutes=i)))

    live = aggregator._windows[("AAPL", "15m")].to_candle("AAPL", "15m")
    assert live.tick_count == 4
    assert validate_candle(live)


def test_aggregator_closes_windows_on_bucket_boundaries_without_losing_volume(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("5m", "1h"))
    t0 = datetime(2026, 9, 23, 10, 3, tzinfo=timezone.utc)

    aggregator._record(make_tick(price=100.0, volume=1_000, timestamp=t0))
    aggregator._record(make_tick(price=102.0, volume=1_200, timestamp=t0 + timedelta(minutes=1)))
    # Crosses into the 10:05 five-minute bucket; same hourly bucket.
    aggregator._record(make_tick(price=101.0, volume=1_500, timestamp=t0 + timedelta(minutes=3)))

    [closed] = aggregator._pending
    assert closed.interval == "5m"
    assert closed.window_start == datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
    assert (closed.open, closed.high, closed.close, closed.volume) == (100.0, 102.0, 102.0, 200)

    # Volume traded between the last tick of one window and the first
    # tick of the next is credited to the new window, not dropped.
    assert aggregator._windows[("AAPL", "5m")].volume == 300
    hour = aggregator._windows[("AAPL", "1h")]
    assert (hour.open, hour.close, hour.volume, hour.tick_count) == (100.0, 101.0, 500, 3)


async def test_get_candles_overlays_unflushed_windows_on_stored_ones(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("5m",))
    now = datetime.now(timezone.utc)
    current = bucket_start(now, "5m")
    earlier = current - timedelta(minutes=5)
    aggregator._replace_rows("AAPL", "5m", [
        _candle("AAPL", "5m", earlier, 90, 95, 89, 94, 10),
        _candle("AAPL", "5m", current, 94, 96, 93, 95, 10),  # stale copy of the open window
    ])

    aggregator._record(make_tick(price=97.0, volume=10, timestamp=now))

    candles = await aggregator.get_candles("AAPL", "5m", earlier)
    assert [c.window_start for c in candles] == [earlier, current]
    assert candles[0].close == 94
    assert candles[1].close == 97.0  # in-memory window wins over the stored row


class _HistoryConnector:
    def __init__(self, symbols, history):
        self.symbols = symbols
        self._history = history

    async def fetch_history(self, symbol, interval, start=None):
        return self._history.get((symbol, interval), [])


async def test_backfill_replaces_stored_history_and_seeds_the_open_window(tmp_path):
    db_path = tmp_path / "c.db"
    aggregator = MarketAggregator(_no_feed(), db_path=db_path, intervals=("5m",))
    now = datetime.now(timezone.utc)
    current = bucket_start(now, "5m")
    earlier = current - timedelta(minutes=5)

    # A previous run's candle for the same window, with a different price path.
    aggregator._replace_rows("AAPL", "5m", [_candle("AAPL", "5m", earlier, 500, 500, 500, 500, 1)])

    history = [
        _candle("AAPL", "5m", earlier, 90, 95, 89, 94, 10),
        _candle("AAPL", "5m", current, 94, 99, 93, 95, 20),
    ]
    stored = await aggregator.backfill(_HistoryConnector(["AAPL"], {("AAPL", "5m"): history}), now=now)

    assert stored == 2
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT window_start, close FROM market_candles WHERE symbol = 'AAPL' ORDER BY window_start"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [(earlier.isoformat(), 94), (current.isoformat(), 95)]

    seeded = aggregator._windows[("AAPL", "5m")]
    assert (seeded.window_start, seeded.open, seeded.high, seeded.volume) == (current, 94, 99, 20)


# ---------------------------------------------------------------- mock history

async def test_mock_history_is_continuous_and_matches_the_live_quote():
    connector = MockMarketConnector(symbols=["AAPL"], interval_seconds=0.01)
    await connector.connect()
    try:
        price = connector._prices["AAPL"]
        profile = connector._profiles["AAPL"]
        session = connector._session["AAPL"]

        five = await connector.fetch_history("AAPL", "5m")
        daily = await connector.fetch_history("AAPL", "1d")
        weekly = await connector.fetch_history("AAPL", "1w")

        # Every interval ends exactly at the live starting price...
        for candles in (five, daily, weekly):
            assert candles[-1].close == price
        # ...and is one unbroken path (each bar opens where the last closed).
        assert all(b.open == a.close for a, b in zip(five, five[1:]))
        assert all(b.open == a.close for a, b in zip(daily, daily[1:]))
        assert all(c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) for c in five)
        # Every interval of the synthetic history passes the OHLCV checks;
        # it came from no ticks, so tick_count is 0 throughout.
        for interval in ("5m", "15m", "1h", "1d", "1w"):
            candles = await connector.fetch_history("AAPL", interval)
            assert all(validate_candle(c) for c in candles), interval
            assert all(c.tick_count == 0 for c in candles)

        midnight = bucket_start(datetime.now(timezone.utc), "1d")
        assert profile["previous_close"] == daily[-2].close
        assert session["open"] == daily[-1].open
        assert daily[-1].window_start == midnight
        assert profile["week52_low"] <= price <= profile["week52_high"]

        since = await connector.fetch_history("AAPL", "1h", midnight)
        assert since[0].window_start == midnight
    finally:
        await connector.disconnect()


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

@pytest.mark.parametrize("range_", list(RANGES))
def test_candles_api_serves_every_range(client, range_):
    response = client.get("/candles", params={"symbol": "aapl", "range": range_})

    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAPL"
    assert body["interval"] == RANGES[range_].interval
    candles = body["candles"]
    assert len(candles) > 1
    times = [c["time"] for c in candles]
    assert times == sorted(times)

    latest = client.get("/market/AAPL").json()
    # The newest candle is the live one, not the last backfilled bar.
    assert candles[-1]["close"] == pytest.approx(latest["price"], abs=5)


def test_candles_api_rejects_unknown_inputs(client):
    assert client.get("/candles", params={"symbol": "NOPE"}).status_code == 404
    assert client.get("/candles", params={"symbol": "AAPL", "range": "2D"}).status_code == 400
    assert client.get("/candles", params={"symbol": "AAPL", "interval": "3m"}).status_code == 400


def test_csv_export_still_serves_15m_candles(client):
    response = client.get("/candles/export", params={"symbol": "AAPL"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    lines = response.text.strip().splitlines()
    assert lines[0] == "symbol,interval,window_start,window_end,open,high,low,close,volume,tick_count"
    assert len(lines) > 1
    assert all(line.startswith("AAPL,15m,") for line in lines[1:])


def test_stock_page_is_served_for_tracked_symbols_only(client):
    assert client.get("/stock/aapl").status_code == 200
    assert client.get("/stock/NOPE").status_code == 404
    assert client.get("/stock", follow_redirects=False).headers["location"] == "/stock/AAPL"
