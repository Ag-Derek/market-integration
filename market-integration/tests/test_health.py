import asyncio
import logging

import pytest

from app.aggregation.market_aggregator import MarketAggregator
from app.queue.market_buffer import MarketDataBuffer


def test_health_reports_all_components_after_startup(client):
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["components"] == {
        "connector": True,
        "buffer": True,
        "processor": True,
        "aggregator": True,
    }


async def test_buffer_reports_unhealthy_and_logs_when_pump_crashes(caplog):
    async def bad_source():
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator

    buffer = MarketDataBuffer(bad_source(), maxsize=10)
    assert buffer.healthy is False  # never started yet

    with caplog.at_level(logging.ERROR):
        await buffer.start()
        for _ in range(50):
            if not buffer.healthy:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("buffer.healthy never flipped to False after the pump crashed")

    # A boolean flag alone doesn't tell you *why* a component died -- the
    # crash must also be logged, not just left for asyncio's default
    # "Task exception was never retrieved" handler to maybe report later.
    assert "Market data buffer pump crashed" in caplog.text

    with pytest.raises(RuntimeError):
        await buffer._pump_task  # retrieve it so it isn't logged as "never retrieved"


async def test_aggregator_reports_unhealthy_and_logs_when_consume_crashes(caplog, tmp_path):
    async def bad_feed():
        raise RuntimeError("boom")
        yield  # pragma: no cover - makes this an async generator

    aggregator = MarketAggregator(
        bad_feed(), db_path=tmp_path / "candles.db", flush_interval_seconds=3600
    )
    assert aggregator.healthy is False  # never started yet

    with caplog.at_level(logging.ERROR):
        await aggregator.start()
        for _ in range(50):
            if not aggregator.healthy:
                break
            await asyncio.sleep(0.02)
        else:
            pytest.fail("aggregator.healthy never flipped to False after the consume loop crashed")

    assert "Market aggregator consume loop crashed" in caplog.text

    with pytest.raises(RuntimeError):
        await aggregator._consume_task  # retrieve it so it isn't logged as "never retrieved"

    aggregator._flush_task.cancel()
    try:
        await aggregator._flush_task
    except asyncio.CancelledError:
        pass
