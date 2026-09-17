from datetime import datetime, timedelta, timezone

from app.validation.market_validator import ValidatingStream, validate_tick


def test_valid_tick_passes(make_tick):
    result = validate_tick(make_tick())

    assert result.is_valid
    assert result.errors == []


def test_bid_greater_than_ask_is_rejected(make_tick):
    tick = make_tick(bid=250.10, ask=250.05)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("bid" in error for error in result.errors)


def test_price_outside_day_range_is_rejected(make_tick):
    tick = make_tick(price=260.00, day_low=248.00, day_high=252.00)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("day range" in error for error in result.errors)


def test_price_outside_52_week_range_is_rejected(make_tick):
    tick = make_tick(price=250.03, day_low=100.00, day_high=400.00, week52_low=150.00, week52_high=200.00)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("52-week range" in error for error in result.errors)


def test_stale_tick_is_rejected(make_tick):
    tick = make_tick(timestamp=datetime.now(timezone.utc) - timedelta(minutes=5))

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("stale" in error for error in result.errors)


def test_future_tick_is_rejected(make_tick):
    tick = make_tick(timestamp=datetime.now(timezone.utc) + timedelta(minutes=1))

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("future" in error for error in result.errors)


async def test_validating_stream_drops_invalid_ticks_and_keeps_valid_ones(make_tick):
    good_1 = make_tick(price=250.00)
    bad = make_tick(bid=300.00, ask=290.00)
    good_2 = make_tick(price=251.00)

    async def feed():
        for tick in (good_1, bad, good_2):
            yield tick

    stream = ValidatingStream(feed())
    seen = [tick async for tick in stream]

    assert seen == [good_1, good_2]
