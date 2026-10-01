"""
The candle and alert branches off the buffer must not lose ticks under
load (#21): the lossless subscription policy, the aggregator flagging
candles when a tick is lost anyway, and /metrics reporting it.
"""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.aggregation.market_aggregator import MarketAggregator
from app.models.candle import bucket_start
from app.queue.market_buffer import MarketDataBuffer


async def _source(ticks):
    for tick in ticks:
        yield tick


async def _no_feed():
    return
    yield  # pragma: no cover - makes this an async generator


async def _wait_for(predicate, timeout=2.0):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    pytest.fail("condition never became true")


# ---------------------------------------------------------------- buffer

async def test_a_slow_lossless_subscriber_gets_every_tick_in_order(make_tick):
    ticks = [make_tick(price=round(6.48 + i * 0.001, 3)) for i in range(50)]
    buffer = MarketDataBuffer(_source(ticks), maxsize=2, lossless_maxsize=5, block_timeout=1.0)
    feed = buffer.subscribe("aggregator", lossless=True)

    await buffer.start()
    seen = []
    try:
        async for tick in feed:
            seen.append(tick)
            await asyncio.sleep(0.001)  # slower than the source
            if len(seen) == len(ticks):
                break
    finally:
        await buffer.stop()

    assert seen == ticks
    assert buffer.dropped_counts == {"aggregator": 0}
    (stats,) = buffer.stats()
    assert stats["policy"] == "lossless" and stats["capacity"] == 5
    assert stats["blocked_seconds"] > 0  # it held the feed back to keep up


async def test_the_display_branch_still_drops_oldest_beside_a_lossless_one(make_tick):
    ticks = [make_tick(price=round(6.48 + i * 0.001, 3)) for i in range(10)]
    buffer = MarketDataBuffer(_source(ticks), maxsize=2, lossless_maxsize=100)
    display = buffer.subscribe("processor")
    candles = buffer.subscribe("aggregator", lossless=True)

    await buffer.start()
    try:
        await _wait_for(lambda: not buffer.healthy)  # source exhausted
        assert buffer.dropped_counts == {"processor": 8, "aggregator": 0}
        assert [await anext(display) for _ in range(2)] == ticks[-2:]
        assert [await anext(candles) for _ in range(10)] == ticks
    finally:
        await buffer.stop()


async def test_a_stalled_lossless_subscriber_costs_one_timeout_then_drops_and_reports(make_tick, caplog):
    ticks = [make_tick(price=round(6.48 + i * 0.001, 3)) for i in range(20)]
    dropped = []
    buffer = MarketDataBuffer(_source(ticks), maxsize=100, lossless_maxsize=5, block_timeout=0.1)
    buffer.subscribe("alerts", lossless=True, on_drop=dropped.append)  # never read
    live = buffer.subscribe("processor")

    loop = asyncio.get_running_loop()
    with caplog.at_level(logging.WARNING, logger="app.queue.market_buffer"):
        started = loop.time()
        await buffer.start()
        try:
            await _wait_for(lambda: not buffer.healthy)
            elapsed = loop.time() - started
        finally:
            await buffer.stop()

    # One wait for room, not one per tick after the queue filled.
    assert elapsed < 0.5
    assert buffer.dropped_counts["alerts"] == 15
    assert dropped == ticks[:15]  # oldest first, as they were lost
    assert buffer.dropped_counts["processor"] == 0
    assert [await anext(live) for _ in range(20)] == ticks
    warnings = [r for r in caplog.records if "Lossless subscriber 'alerts' dropped" in r.getMessage()]
    assert len(warnings) == 1  # throttled, not one line per tick


# ---------------------------------------------------------------- aggregator

def _flagged(aggregator, symbol="MTNGH"):
    return {
        interval
        for (s, interval), w in aggregator._windows.items()
        if s == symbol and w.possibly_incomplete
    }


def test_a_dropped_tick_flags_the_windows_it_belonged_to(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("5m", "1h"))
    now = datetime.now(timezone.utc)
    aggregator._record(make_tick(price=6.50, volume=1_000, timestamp=now))

    aggregator.mark_dropped(make_tick(price=6.59, volume=1_200, timestamp=now))

    assert _flagged(aggregator) == {"5m", "1h"}
    assert aggregator.incomplete_candles == 2


def test_the_tick_after_a_gap_flags_its_windows_too(make_tick, tmp_path, monkeypatch):
    # The lost tick would have opened a new 5m window; the next tick
    # opens it instead, with the lost tick's volume in its delta. Fixed
    # times either side of a 5m boundary, so let them be old.
    monkeypatch.setattr("app.validation.market_validator.MAX_TICK_AGE", timedelta(days=3650))
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("5m", "1w"))
    t0 = datetime(2026, 9, 23, 10, 4, 50, tzinfo=timezone.utc)  # a Wednesday
    aggregator._record(make_tick(price=6.50, volume=1_000, timestamp=t0))
    later = datetime(2026, 9, 23, 10, 5, 1, tzinfo=timezone.utc)

    aggregator.mark_dropped(make_tick(price=6.59, volume=1_200, timestamp=later))
    assert _flagged(aggregator) == {"1w"}  # the 5m window it belongs to isn't open yet

    aggregator._record(make_tick(price=6.55, volume=1_300, timestamp=later))
    assert _flagged(aggregator) == {"5m", "1w"}
    window = aggregator._windows[("MTNGH", "5m")]
    assert window.window_start == bucket_start(later, "5m") and window.volume == 300

    # Only the tick right after the gap.
    aggregator._record(make_tick(price=6.56, volume=1_400, timestamp=later + timedelta(minutes=5)))
    assert _flagged(aggregator) == {"1w"}


def test_dropped_ticks_the_aggregator_would_have_skipped_flag_nothing(make_tick, tmp_path):
    aggregator = MarketAggregator(_no_feed(), db_path=tmp_path / "c.db", intervals=("5m",))
    aggregator._record(make_tick(price=6.50))

    aggregator.mark_dropped(make_tick(bid=6.60, ask=6.55))  # crossed book: invalid

    assert _flagged(aggregator) == set()
    assert aggregator._gap_symbols == set()


async def test_flagged_candles_are_stored_and_read_back(make_tick, tmp_path):
    db_path = tmp_path / "c.db"
    aggregator = MarketAggregator(_no_feed(), db_path=db_path, intervals=("5m",))
    now = datetime.now(timezone.utc)
    aggregator._record(make_tick(price=6.50, timestamp=now))
    aggregator.mark_dropped(make_tick(price=6.59, timestamp=now))

    await aggregator._flush()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT possibly_incomplete FROM market_candles").fetchall() == [(1,)]
    finally:
        conn.close()
    aggregator._windows.clear()  # read from the database alone
    (candle,) = await aggregator.get_candles("MTNGH", "5m")
    assert candle.possibly_incomplete is True


def test_an_older_database_gains_the_possibly_incomplete_column(tmp_path):
    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE market_candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL, interval TEXT NOT NULL,
            window_start TEXT NOT NULL, window_end TEXT NOT NULL,
            open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
            volume INTEGER NOT NULL, tick_count INTEGER NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (symbol, interval, window_start)
        )
        """
    )
    conn.execute(
        "INSERT INTO market_candles (symbol, interval, window_start, window_end, open, high, "
        "low, close, volume, tick_count, created_at) "
        "VALUES ('GCB', '5m', 'a', 'b', 1, 1, 1, 1, 0, 0, 'c')"
    )
    conn.commit()
    conn.close()

    MarketAggregator(_no_feed(), db_path=db_path)

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT possibly_incomplete FROM market_candles").fetchall() == [(0,)]
    finally:
        conn.close()


# ---------------------------------------------------------------- the app

def test_the_app_subscribes_the_aggregator_losslessly(client):
    from app.main import buffer

    policies = {s["subscriber"]: s["policy"] for s in buffer.stats()}
    assert policies == {"processor": "drop_oldest", "aggregator": "lossless"}


def test_metrics_reports_drops_per_subscriber_and_incomplete_candles(client):
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    lines = response.text.splitlines()
    assert 'market_buffer_dropped_ticks_total{subscriber="aggregator"} 0' in lines
    assert any(line.startswith('market_buffer_queue_depth{subscriber="aggregator",mode="lossless"} ')
               for line in lines)
    assert 'market_buffer_queue_capacity{subscriber="aggregator"} 10000' in lines
    assert 'market_buffer_queue_capacity{subscriber="processor"} 200' in lines
    for name in ("market_buffer_queue_peak_depth", "market_buffer_blocked_seconds_total"):
        assert any(line.startswith(f'{name}{{subscriber="aggregator"}} ') for line in lines)
    assert any(line.startswith("market_aggregator_incomplete_candles_total ") for line in lines)

    snapshot = client.get("/metrics", params={"format": "json"}).json()
    aggregator = snapshot["buffer"]["subscribers"]["aggregator"]
    assert aggregator["mode"] == "lossless" and aggregator["capacity"] == 10_000
    assert snapshot["aggregator"]["incomplete_candles"] == 0


def test_candles_api_says_whether_each_candle_is_complete(client):
    response = client.get("/candles", params={"symbol": "MTNGH", "range": "1D"})

    assert response.status_code == 200
    candles = response.json()["candles"]
    assert candles and all(c["possibly_incomplete"] is False for c in candles)
