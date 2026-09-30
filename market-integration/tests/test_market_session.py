"""Market session awareness and an accurate "Live" badge (#14)."""

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.connectors.market_connector import MockMarketConnector
from app.gateways.websocket_gateway import WebSocketGateway
from app.session import CALENDAR_PATH
from app.session.calendar import MarketCalendar, SessionState, load_calendar
from app.session.status import Badge, FeedState, badge, feed_state, market_status
from app.validation.market_validator import validate_tick

UTC = timezone.utc
STALE_AFTER = timedelta(seconds=15)


def _calendar(override=None, holidays=None) -> MarketCalendar:
    return MarketCalendar(
        tz=UTC,
        timezone_name="Africa/Accra",
        trading_days={0, 1, 2, 3, 4},
        pre_open=time(9, 30),
        open=time(10, 0),
        close=time(15, 0),
        holidays=holidays if holidays is not None else {
            date(2026, 12, 25): "Christmas Day",
            date(2026, 12, 28): "Boxing Day (observed)",
        },
        override=override,
    )


def _at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00+00:00")


# ---------------------------------------------------------------- calendar

@pytest.mark.parametrize("hhmm,state,reason", [
    ("08:00", SessionState.CLOSED, "before_hours"),
    ("09:30", SessionState.PRE_OPEN, "pre_open"),
    ("09:59", SessionState.PRE_OPEN, "pre_open"),
    ("10:00", SessionState.OPEN, None),
    ("14:59", SessionState.OPEN, None),
    ("15:00", SessionState.CLOSED, "after_hours"),
])
def test_session_state_through_a_trading_day(hhmm, state, reason):
    status = _calendar().status(_at("2026-09-30", hhmm))  # a Wednesday

    assert (status.state, status.reason) == (state, reason)


def test_while_open_next_close_is_today_and_next_open_is_tomorrow():
    status = _calendar().status(_at("2026-09-30", "11:00"))

    assert status.next_close == _at("2026-09-30", "15:00")
    assert status.next_open == _at("2026-10-01", "10:00")
    assert status.last_close == _at("2026-09-29", "15:00")


def test_before_the_open_the_next_session_is_today():
    status = _calendar().status(_at("2026-09-30", "09:45"))

    assert status.next_open == _at("2026-09-30", "10:00")
    assert status.next_close == _at("2026-09-30", "15:00")
    assert status.last_close == _at("2026-09-29", "15:00")


def test_friday_after_the_close_the_next_session_is_monday():
    status = _calendar().status(_at("2026-10-02", "16:00"))

    assert status.last_close == _at("2026-10-02", "15:00")
    assert status.next_open == _at("2026-10-05", "10:00")


def test_weekend_is_closed_with_friday_as_the_last_close():
    status = _calendar().status(_at("2026-10-03", "12:00"))  # Saturday

    assert (status.state, status.reason) == (SessionState.CLOSED, "weekend")
    assert status.last_close == _at("2026-10-02", "15:00")
    assert status.next_open == _at("2026-10-05", "10:00")


def test_a_public_holiday_is_closed_and_skipped_for_the_next_open():
    # Christmas is a Friday; Boxing Day is observed on the Monday.
    status = _calendar().status(_at("2026-12-25", "11:00"))

    assert (status.state, status.reason, status.holiday) == (SessionState.CLOSED, "holiday", "Christmas Day")
    assert status.last_close == _at("2026-12-24", "15:00")
    assert status.next_open == _at("2026-12-29", "10:00")


def test_override_pins_the_state_but_keeps_the_real_times():
    status = _calendar(override=SessionState.OPEN).status(_at("2026-10-03", "12:00"))

    assert (status.state, status.reason) == (SessionState.OPEN, "override")
    assert status.next_open == _at("2026-10-05", "10:00")


def test_the_checked_in_calendar_loads():
    calendar = load_calendar(CALENDAR_PATH)

    assert calendar.describe()["timezone"] == "Africa/Accra"
    assert not calendar.is_trading_day(date(2026, 12, 25))
    assert calendar.is_trading_day(date(2026, 9, 30))


@pytest.mark.parametrize("change,message", [
    ({"open": "10am"}, "open must be HH:MM"),
    ({"trading_days": ["mon", "funday"]}, "unknown trading_days"),
    ({"holidays": [{"date": "2026-12-25"}, {"date": "2026-12-25"}]}, "listed twice"),
    ({"open": "16:00"}, "pre_open <= open < close"),
])
def test_a_malformed_calendar_fails_loudly(tmp_path, change, message):
    raw = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    raw.update(change)
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_calendar(path)


def test_an_unknown_override_fails_loudly():
    with pytest.raises(ValueError, match="MARKET_SESSION_OVERRIDE"):
        load_calendar(CALENDAR_PATH, override="sideways")


# ---------------------------------------------------------------- badge

def test_feed_state_follows_the_heartbeat_not_trades():
    now = _at("2026-09-30", "11:00")

    assert feed_state(True, now - timedelta(seconds=2), now, STALE_AFTER) is FeedState.OK
    assert feed_state(True, now - timedelta(seconds=60), now, STALE_AFTER) is FeedState.DELAYED
    assert feed_state(True, None, now, STALE_AFTER) is FeedState.DOWN
    assert feed_state(False, now, now, STALE_AFTER) is FeedState.DOWN


@pytest.mark.parametrize("session,feed,expected", [
    (SessionState.OPEN, FeedState.OK, Badge.LIVE),
    (SessionState.OPEN, FeedState.DELAYED, Badge.DELAYED),
    (SessionState.OPEN, FeedState.DOWN, Badge.DISCONNECTED),
    (SessionState.PRE_OPEN, FeedState.OK, Badge.CLOSED),
    (SessionState.CLOSED, FeedState.OK, Badge.CLOSED),
    (SessionState.CLOSED, FeedState.DELAYED, Badge.CLOSED),
    (SessionState.CLOSED, FeedState.DOWN, Badge.DISCONNECTED),
])
def test_badge(session, feed, expected):
    assert badge(session, feed) is expected


def test_status_payload_outside_hours_says_closed_with_the_last_close():
    now = _at("2026-10-03", "12:00")

    body = market_status(_calendar(), running=True, last_heartbeat=now, now=now, stale_after=STALE_AFTER)

    assert body["status"] == "closed" and body["badge"] == "closed"
    assert body["last_close"] == "2026-10-02T15:00:00+00:00"
    assert body["next_open"] == "2026-10-05T10:00:00+00:00"
    assert body["feed"]["state"] == "ok"


# ---------------------------------------------------------------- last_trade_at

def test_an_hours_old_last_trade_on_a_fresh_quote_is_valid(make_tick):
    now = datetime.now(UTC)
    tick = make_tick(timestamp=now, last_trade_at=now - timedelta(days=3))

    assert validate_tick(tick).is_valid


def test_a_last_trade_after_the_quote_is_rejected(make_tick):
    now = datetime.now(UTC)
    tick = make_tick(timestamp=now, last_trade_at=now + timedelta(minutes=5))

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("last_trade_at" in e for e in result.errors)


def test_session_volume_without_a_last_trade_is_rejected(make_tick):
    result = validate_tick(make_tick(volume=500, last_trade_at=None))

    assert not result.is_valid
    assert any("no last_trade_at" in e for e in result.errors)


def test_a_never_traded_name_has_no_last_trade(make_tick):
    tick = make_tick(
        price=6.40, vwap=6.40, change=0, day_low=6.40, day_high=6.40,
        volume=0, value_traded=0, last_trade_at=None,
    )

    assert validate_tick(tick).is_valid


# ---------------------------------------------------------------- mock connector

async def _anext(stream, timeout):
    return await asyncio.wait_for(stream.__anext__(), timeout)


async def test_mock_does_not_trade_while_closed_but_keeps_its_heartbeat():
    connector = MockMarketConnector(
        symbols=["MTNGH"], interval_seconds=0.01, calendar=_calendar(override=SessionState.CLOSED),
    )
    await connector.connect()
    stream = connector.stream()
    try:
        snapshot = await _anext(stream, 1)  # every symbol's opening quote
        assert snapshot.symbol == "MTNGH"
        before = connector.last_heartbeat
        # MTNGH trades every few seconds while open; closed, nothing comes.
        with pytest.raises(asyncio.TimeoutError):
            await _anext(stream, 0.5)
        assert connector.last_heartbeat > before
    finally:
        await connector.disconnect()
        await stream.aclose()


async def test_mock_quotes_carry_a_last_trade_time_distinct_from_the_timestamp():
    connector = MockMarketConnector(symbols=["MTNGH"], interval_seconds=0.01)
    await connector.connect()
    try:
        quote = connector.normalize(connector._quote("MTNGH", connector._profiles["MTNGH"]))
        assert quote.last_trade_at is not None and quote.last_trade_at <= quote.timestamp
        assert validate_tick(quote).is_valid
    finally:
        await connector.disconnect()


async def test_mock_rolls_into_a_new_session_at_the_open():
    connector = MockMarketConnector(
        symbols=["MTNGH"], interval_seconds=0.01, calendar=_calendar(override=SessionState.OPEN),
    )
    await connector.connect()
    try:
        connector._session_day = connector._session_day - timedelta(days=1)
        session = connector._session["MTNGH"]
        session.update(volume=1_000, value=6_500.0)
        last_trade = connector._last_trade_at["MTNGH"]

        assert connector._trading(datetime.now(UTC))

        profile = connector._profiles["MTNGH"]
        assert profile["previous_close"] == 6.50       # yesterday's VWAP
        assert connector._session["MTNGH"]["volume"] == 0
        assert connector._session["MTNGH"]["open"] == 6.50
        quote = connector.normalize(connector._quote("MTNGH", profile))
        assert quote.change == 0 and quote.last_trade_at == last_trade
        assert validate_tick(quote).is_valid
    finally:
        await connector.disconnect()


# ---------------------------------------------------------------- delivery

class _Socket:
    def __init__(self):
        self.sent = []

    async def accept(self):
        pass

    async def send_json(self, message):
        self.sent.append(message)


async def test_status_goes_to_every_client_and_only_the_newest_is_sent():
    gateway = WebSocketGateway(["MTNGH"], snapshot=lambda s: {}, status=lambda: {"badge": "closed"})
    socket = _Socket()
    await gateway.connect(socket)  # never subscribes to anything

    await gateway.broadcast_status({"badge": "closed"})
    await gateway.broadcast_status({"badge": "live"})
    for _ in range(20):
        await asyncio.sleep(0)

    assert socket.sent[0] == {"type": "welcome", "symbols": ["MTNGH"], "status": {"badge": "closed"}}
    assert socket.sent[1:] == [{"type": "status", "badge": "live"}]


def test_market_status_endpoint(client):
    body = client.get("/market/status").json()

    # The suite pins the session open (conftest) and the mock beats its
    # heartbeat every loop.
    assert body["status"] == "open"
    assert body["badge"] == "live"
    assert body["feed"]["state"] == "ok"
    for key in ("next_open", "next_close", "last_close"):
        datetime.fromisoformat(body[key])
    assert body["session"]["timezone"] == "Africa/Accra"
