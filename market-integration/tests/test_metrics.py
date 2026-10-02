import asyncio
from datetime import datetime, timedelta, timezone

from app import metrics
from app.aggregation.market_aggregator import MarketAggregator
from app.queue.market_buffer import MarketDataBuffer
from app.validation.market_validator import RejectionCounter, validate_tick


# ---------------------------------------------------------------- validation by rule

def test_each_error_names_its_rule(make_tick):
    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    tick = make_tick(bid=7.0, ask=6.0, timestamp=stale, last_trade_at=stale)
    result = validate_tick(tick)
    assert not result
    # Rules line up with their messages, one each.
    by_rule = dict(zip(result.rules, result.errors))
    assert list(by_rule) == ["crossed_book", "stale"]
    assert "not less than ask" in by_rule["crossed_book"]
    assert "is stale" in by_rule["stale"]
    assert validate_tick(make_tick()).rules == []


def test_rejection_counter_counts_ticks_once_and_rules_each(make_tick):
    counter = RejectionCounter()
    stale = datetime.now(timezone.utc) - timedelta(minutes=5)
    counter.record(validate_tick(make_tick()))
    counter.record(validate_tick(make_tick(bid=7.0, ask=6.0)))
    counter.record(validate_tick(make_tick(bid=7.0, ask=6.0, timestamp=stale, last_trade_at=stale)))
    snap = counter.snapshot()
    assert (snap["passed"], snap["rejected"]) == (1, 2)
    assert snap["by_rule"]["crossed_book"] == 2
    assert snap["by_rule"]["stale"] == 1


# ---------------------------------------------------------------- components

async def test_buffer_reports_received_ticks_and_queue_depth_per_subscriber(make_tick):
    async def source():
        for i in range(5):
            yield make_tick("MTNGH" if i % 2 else "GCB", volume=i)

    buffer = MarketDataBuffer(source(), maxsize=3)
    buffer.subscribe("slow")             # never read: fills, then drops
    buffer.subscribe_latest("latest")
    await buffer.start()
    for _ in range(50):
        if buffer.received == 5:
            break
        await asyncio.sleep(0.01)
    await buffer.stop()

    assert buffer.received == 5
    assert buffer.queue_depths == {"slow": 3, "latest": 2}  # two symbols unread
    assert buffer.dropped_counts["slow"] == 2
    assert buffer.subscriber_modes == {"slow": "queue", "latest": "latest"}


async def test_aggregator_records_its_last_flush(make_tick, tmp_path):
    async def feed():
        yield make_tick()

    aggregator = MarketAggregator(feed(), db_path=tmp_path / "db.sqlite", flush_interval_seconds=3600)
    assert (aggregator.flushes, aggregator.last_flush_at) == (0, None)
    await aggregator.start()
    await asyncio.sleep(0.05)
    await aggregator.stop()  # final flush

    assert aggregator.flushes == 1
    assert aggregator.last_flush_rows > 0
    assert aggregator.last_flush_seconds >= 0
    assert datetime.now(timezone.utc) - aggregator.last_flush_at < timedelta(seconds=5)
    assert aggregator.rejections.passed == 1


# ---------------------------------------------------------------- Prometheus

SNAPSHOT = {
    "buffer": {"received": 10, "capacity": 200, "subscribers": {
        "processor": {"mode": "queue", "depth": 4, "dropped": 2},
    }},
    "websocket": {"clients": 3, "subscriptions": 7, "conflated": 0},
    "validation": {"processor": {"passed": 8, "rejected": 2, "by_rule": {"stale": 2}}},
    "aggregator": {"flushes": 5, "last_flush_at": "2026-10-01T12:00:00+00:00",
                   "last_flush_seconds": 0.012, "last_flush_rows": 40, "incomplete_candles": 0},
    "feeds": {"equities": {"state": "connected", "up": True, "reconnects": 1, "attempt": 0}},
}


def test_prometheus_exposition():
    text = metrics.to_prometheus(SNAPSHOT)
    lines = text.splitlines()
    assert text.endswith("\n")
    assert "# TYPE market_buffer_dropped_ticks_total counter" in lines
    assert 'market_buffer_dropped_ticks_total{subscriber="processor"} 2' in lines
    assert 'market_buffer_queue_depth{subscriber="processor",mode="queue"} 4' in lines
    assert "market_websocket_clients 3" in lines
    assert 'market_validation_rejections_total{consumer="processor",rule="stale"} 2' in lines
    assert 'market_validation_ticks_total{consumer="processor",result="passed"} 8' in lines
    assert "market_aggregator_last_flush_duration_seconds 0.012" in lines
    assert "market_aggregator_last_flush_timestamp_seconds 1790856000" in lines  # exact, not rounded
    assert 'market_feed_up{feed="equities"} 1' in lines
    # Every sample belongs to a declared metric.
    declared = {line.split()[2] for line in lines if line.startswith("# TYPE")}
    for line in lines:
        if not line.startswith("#"):
            assert line.split("{")[0].split(" ")[0] in declared, line


def test_prometheus_omits_flush_gauges_before_the_first_flush():
    snapshot = {**SNAPSHOT, "aggregator": {"flushes": 0, "last_flush_at": None,
                                           "last_flush_seconds": None, "last_flush_rows": None,
                                           "incomplete_candles": 0}}
    text = metrics.to_prometheus(snapshot)
    assert "market_aggregator_flushes_total 0" in text
    assert "last_flush_duration" not in text


def test_prometheus_label_values_are_escaped():
    assert metrics._labels({"a": 'say "hi"\\now\nnext'}) == '{a="say \\"hi\\"\\\\now\\nnext"}'


def test_health_summary():
    assert metrics.summary(SNAPSHOT) == {
        "dropped_ticks": 2, "max_queue_depth": 4, "queue_capacity": 200, "websocket_clients": 3,
        "rejected_ticks": {"processor": 2},
        "last_flush_at": "2026-10-01T12:00:00+00:00", "last_flush_seconds": 0.012,
    }


# ---------------------------------------------------------------- API
# `client` is the session-wide running app from conftest.py.

def test_metrics_endpoint_serves_prometheus_text_by_default(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for name in ("market_buffer_dropped_ticks_total", "market_buffer_queue_depth",
                 "market_websocket_clients", "market_validation_ticks_total", "market_feed_up"):
        assert f"# TYPE {name} " in response.text
    assert 'subscriber="processor"' in response.text and 'subscriber="aggregator"' in response.text


def test_metrics_endpoint_json_counts_websocket_clients(client):
    before = client.get("/metrics", params={"format": "json"}).json()
    assert set(before) == {"buffer", "websocket", "validation", "aggregator", "feeds"}
    assert set(before["buffer"]["subscribers"]) == {"processor", "aggregator"}
    assert set(before["validation"]) == {"processor", "aggregator"}
    assert before["buffer"]["received"] > 0
    assert before["validation"]["processor"]["passed"] > 0

    with client.websocket_connect("/ws/market") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"action": "subscribe", "symbols": ["MTNGH", "GCB"]})
        ws.receive_json()  # subscribed
        during = client.get("/metrics", params={"format": "json"}).json()["websocket"]
    assert during["clients"] == before["websocket"]["clients"] + 1
    assert during["subscriptions"] >= before["websocket"]["subscriptions"] + 2

    assert client.get("/metrics", params={"format": "xml"}).status_code == 422


def test_health_includes_a_metrics_summary(client):
    summary = client.get("/health").json()["metrics"]
    assert set(summary) == {"dropped_ticks", "max_queue_depth", "queue_capacity", "websocket_clients",
                            "rejected_ticks", "last_flush_at", "last_flush_seconds"}
    assert set(summary["rejected_ticks"]) == {"processor", "aggregator"}
