import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from app.aggregation.market_aggregator import MarketAggregator
from app.processors.market_processor import MarketProcessor
from app.queue.market_buffer import MarketDataBuffer
from app.validation.market_validator import ValidatingStream


async def _source(ticks):
    for tick in ticks:
        yield tick


async def test_full_pipeline_delivers_only_validated_ticks(make_tick):
    """connector -> buffer -> validator -> processor -> broadcast, wired
    the way app.main wires it. A bad tick (bid >= ask) must never reach
    the processor's latest-state or the broadcast callback."""
    good_1 = make_tick(price=6.54)
    bad = make_tick(bid=6.60, ask=6.55)
    good_2 = make_tick(price=6.55)

    buffer = MarketDataBuffer(_source([good_1, bad, good_2]), maxsize=10)
    processor = MarketProcessor()
    validated_feed = ValidatingStream(buffer.subscribe("processor"))

    broadcasted: list = []
    done = asyncio.Event()

    async def on_processed(tick):
        broadcasted.append(tick)
        if len(broadcasted) == 2:
            done.set()

    await buffer.start()
    consume_task = asyncio.create_task(processor.consume(validated_feed, on_processed=on_processed))
    try:
        await asyncio.wait_for(done.wait(), timeout=2)
    finally:
        consume_task.cancel()
        await buffer.stop()

    assert [tick.price for tick in broadcasted] == [6.54, 6.55]
    assert processor.get_latest(good_1.symbol).price == 6.55


async def test_aggregator_persists_ohlcv_for_valid_ticks_only(make_tick, tmp_path):
    t0 = datetime.now(timezone.utc)
    good_1 = make_tick(price=6.54, volume=1000, timestamp=t0)
    bad = make_tick(bid=6.60, ask=6.55, timestamp=t0)
    good_2 = make_tick(price=6.56, volume=1500, timestamp=t0)

    buffer = MarketDataBuffer(_source([good_1, bad, good_2]), maxsize=10)
    db_path = tmp_path / "candles.db"
    aggregator = MarketAggregator(
        buffer.subscribe("aggregator"), db_path=db_path, flush_interval_seconds=3600
    )

    await buffer.start()
    await aggregator.start()
    try:
        for _ in range(50):
            window = aggregator._windows.get((good_1.symbol, "15m"))
            if window is not None and window.tick_count == 2:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("aggregator never recorded both valid ticks")

        await aggregator._flush()
    finally:
        await aggregator.stop(final_flush=False)
        await buffer.stop()

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT open, high, low, close, volume, tick_count FROM market_candles "
            "WHERE symbol = ? AND interval = '15m'",
            (good_1.symbol,),
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 1
    open_, high, low, close, volume, tick_count = rows[0]
    assert (open_, close) == (6.54, 6.56)
    assert tick_count == 2  # the bad tick was dropped before recording
    assert volume == 500  # cumulative-volume delta: 1500 - 1000


async def test_buffer_drops_oldest_when_a_subscriber_falls_behind(make_tick):
    ticks = [make_tick(price=round(6.40 + i * 0.01, 2)) for i in range(10)]
    buffer = MarketDataBuffer(_source(ticks), maxsize=2)
    feed = buffer.subscribe("slow")

    await buffer.start()
    try:
        for _ in range(50):
            if buffer.dropped_counts.get("slow", 0) >= 8:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("expected the slow subscriber to lose ticks to backpressure")

        seen = []
        async for tick in feed:
            seen.append(tick)
            if len(seen) == 2:
                break

        # Drop-oldest means the newest tick always survives.
        assert seen[-1].price == 6.49
    finally:
        await buffer.stop()
