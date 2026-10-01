import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.connectors.base_connector import BaseMarketConnector
from app.connectors.supervisor import CONNECTED, RECONNECTING, STOPPED, SupervisedConnector
from app.session.calendar import SessionState
from app.session.status import Badge, FeedState, badge, feed_state


class FlakyConnector(BaseMarketConnector):
    """Fails its first `connect_failures` connects; each stream yields
    `ticks_per_stream` ticks, then drops (raises) for the first
    `stream_drops` streams and ends cleanly after that."""

    def __init__(self, make_tick, connect_failures=0, stream_drops=0, ticks_per_stream=3):
        super().__init__(["FLAKY"])
        self._make_tick = make_tick
        self.connect_failures = connect_failures
        self.stream_drops = stream_drops
        self.ticks_per_stream = ticks_per_stream
        self.connects = 0
        self.disconnects = 0
        self.sent = 0

    async def connect(self):
        self.connects += 1
        if self.connect_failures:
            self.connect_failures -= 1
            raise ConnectionError("provider unreachable")
        self.running = True
        self.last_heartbeat = datetime.now(timezone.utc)

    async def disconnect(self):
        self.disconnects += 1
        self.running = False

    async def stream(self):
        for _ in range(self.ticks_per_stream):
            self.sent += 1
            yield self._make_tick("FLAKY", volume=self.sent)
        if self.stream_drops:
            self.stream_drops -= 1
            raise ConnectionResetError("socket closed by peer")
        # Then a quiet but healthy feed, until disconnected.
        while self.running:
            await asyncio.sleep(0)


def _supervise(connector, **kw):
    delays, states = [], []

    async def fake_sleep(seconds):
        delays.append(seconds)

    feed = SupervisedConnector(
        connector, name="test", initial_delay=1.0, max_delay=8.0,
        on_change=lambda f: states.append(f.state), sleep=fake_sleep,
        random_fraction=lambda: 0.0, **kw,
    )
    return feed, delays, states


async def _take(stream, n):
    out = []
    async for tick in stream:
        out.append(tick)
        if len(out) == n:
            break
    return out


# ---------------------------------------------------------------- backoff

def test_backoff_doubles_up_to_the_cap():
    feed = SupervisedConnector(FlakyConnector(None), name="x",
                               initial_delay=1, max_delay=8, jitter=0, random_fraction=lambda: 0.5)
    assert [feed.backoff(n) for n in range(1, 7)] == [1, 2, 4, 8, 8, 8]


@pytest.mark.parametrize("fraction,expected", [(0.0, 8.0), (0.5, 6.0), (1.0, 4.0)])
def test_jitter_takes_up_to_its_share_off_the_delay(fraction, expected):
    feed = SupervisedConnector(FlakyConnector(None), name="x",
                               initial_delay=1, max_delay=8, jitter=0.5, random_fraction=lambda: fraction)
    assert feed.backoff(10) == expected  # never above max_delay


def test_bad_settings_are_rejected():
    inner = FlakyConnector(None)
    with pytest.raises(ValueError):
        SupervisedConnector(inner, name="x", initial_delay=0)
    with pytest.raises(ValueError):
        SupervisedConnector(inner, name="x", initial_delay=5, max_delay=1)
    with pytest.raises(ValueError):
        SupervisedConnector(inner, name="x", jitter=1.5)


# ---------------------------------------------------------------- recovery

async def test_a_connect_that_fails_n_times_is_retried_with_backoff_then_recovers(make_tick):
    flaky = FlakyConnector(make_tick, connect_failures=3)
    feed, delays, states = _supervise(flaky)

    stream = feed.stream()
    ticks = await _take(stream, 3)
    await stream.aclose()

    assert [t.symbol for t in ticks] == ["FLAKY"] * 3
    assert flaky.connects == 4
    assert delays == [1.0, 2.0, 4.0]
    assert RECONNECTING in states and states[-1] == CONNECTED
    # Ticks flowing again resets the count.
    assert (feed.attempt, feed.reconnects, feed.next_retry_at) == (0, 1, None)
    assert "provider unreachable" in feed.last_error


async def test_a_stream_that_drops_mid_stream_reconnects_and_carries_on(make_tick):
    flaky = FlakyConnector(make_tick, stream_drops=2, ticks_per_stream=2)
    feed, delays, _ = _supervise(flaky)
    await feed.connect()

    stream = feed.stream()
    ticks = await _take(stream, 6)
    await stream.aclose()

    # 2 ticks, drop, 2 ticks, drop, 2 ticks: nothing lost between drops.
    assert [t.volume for t in ticks] == [1, 2, 3, 4, 5, 6]
    assert (flaky.connects, flaky.disconnects) == (3, 2)
    # Each drop was followed by ticks, so each retry is a first retry.
    assert delays == [1.0, 1.0]
    assert feed.reconnects == 2
    assert "socket closed by peer" in feed.last_error


async def test_a_provider_that_keeps_dropping_straight_away_still_backs_off(make_tick):
    flaky = FlakyConnector(make_tick, stream_drops=4, ticks_per_stream=0)
    feed, delays, _ = _supervise(flaky)
    await feed.connect()

    # Connects fine, drops before any tick: the count must not reset.
    stream = feed.stream()
    task = asyncio.ensure_future(stream.__anext__())
    for _ in range(100):
        await asyncio.sleep(0)
        if len(delays) >= 4:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert delays[:4] == [1.0, 2.0, 4.0, 8.0]


async def test_killing_the_connector_mid_stream_is_a_drop(make_tick):
    flaky = FlakyConnector(make_tick, ticks_per_stream=1)
    feed, delays, _ = _supervise(flaky)
    await feed.connect()

    stream = feed.stream()
    assert len(await _take(stream, 1)) == 1
    await flaky.disconnect()  # the connector itself, not the supervisor
    assert len(await _take(stream, 1)) == 1
    await stream.aclose()
    assert delays == [1.0]
    assert feed.last_error == "stream ended"


async def test_disconnecting_the_supervisor_stops_for_good(make_tick):
    flaky = FlakyConnector(make_tick, ticks_per_stream=1)
    feed, delays, _ = _supervise(flaky)
    await feed.connect()

    stream = feed.stream()
    await _take(stream, 1)
    await feed.disconnect()
    assert [t async for t in stream] == []  # ends instead of retrying
    assert delays == [] and feed.state == STOPPED


# ---------------------------------------------------------------- status

def test_a_feed_being_retried_reports_reconnecting_but_badges_disconnected():
    now = datetime.now(timezone.utc)
    stale = timedelta(seconds=15)
    assert feed_state(False, now, now, stale, reconnecting=True) is FeedState.RECONNECTING
    assert feed_state(False, now, now, stale) is FeedState.DOWN
    # Only a feed that is actually down is "reconnecting".
    assert feed_state(True, now, now, stale, reconnecting=True) is FeedState.OK
    assert badge(SessionState.OPEN, FeedState.RECONNECTING) is Badge.DISCONNECTED
    assert badge(SessionState.CLOSED, FeedState.RECONNECTING) is Badge.DISCONNECTED


# ---------------------------------------------------------------- the service
# `client` is the session-wide running app from conftest.py.

def _wait_for(check, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail("timed out waiting for the service")


def test_killing_the_equities_connector_recovers_without_a_restart(client):
    from app.main import equities, equities_feed, processor

    before = equities_feed.reconnects
    asyncio.run(equities.disconnect())  # kill the provider connection mid-stream

    def reconnecting():
        body = client.get("/health").json()
        return body if body["status"] == "reconnecting" else None

    body = reconnecting() or _wait_for(reconnecting)
    assert body["feeds"]["equities"]["state"] in ("reconnecting", "connecting")
    assert body["feeds"]["fixed_income"]["state"] == "connected"  # untouched
    status = client.get("/market/status").json()
    if status["feed"]["state"] == "reconnecting":  # unless it already recovered
        assert status["badge"] == "disconnected"
        assert status["feed"]["reconnect"]["feed"] == "equities"

    _wait_for(lambda: client.get("/health").json()["status"] == "healthy")
    assert equities_feed.reconnects == before + 1
    assert client.get("/health").status_code == 200

    # And quotes flow again.
    seen = processor.get_latest("MTNGH").timestamp
    _wait_for(lambda: processor.get_latest("MTNGH").timestamp > seen)
