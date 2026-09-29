from datetime import datetime, timedelta, timezone

import pytest

from app.validation.market_validator import ValidatingStream, validate_tick


def test_valid_tick_passes(make_tick):
    result = validate_tick(make_tick())

    assert result.is_valid
    assert result.errors == []


def test_bid_greater_than_ask_is_rejected(make_tick):
    tick = make_tick(bid=6.56, ask=6.55)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("bid" in error for error in result.errors)


@pytest.mark.parametrize("bid,ask", [(6.54, None), (None, 6.55), (None, None)])
def test_one_sided_or_empty_book_is_valid(make_tick, bid, ask):
    # Normal on the GSE: many names close with only a bid, only an
    # offer, or no resting orders at all.
    tick = make_tick(
        bid=bid, bid_size=100 if bid else 0,
        ask=ask, ask_size=100 if ask else 0,
    )

    assert validate_tick(tick).is_valid


def test_price_outside_day_range_is_rejected(make_tick):
    tick = make_tick(price=6.80, day_low=6.48, day_high=6.60)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("day range" in error for error in result.errors)


def test_last_trade_outside_day_range_is_rejected(make_tick):
    tick = make_tick(last_trade_price=6.70, day_low=6.48, day_high=6.60)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("last_trade_price" in error for error in result.errors)


def test_price_outside_52_week_range_is_rejected(make_tick):
    tick = make_tick(price=6.54, day_low=4.00, day_high=8.00, week52_low=4.20, week52_high=6.00)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("52-week range" in error for error in result.errors)


def test_price_outside_year_range_is_rejected(make_tick):
    tick = make_tick(price=6.54, day_low=4.00, day_high=8.00, year_low=4.20, year_high=6.00)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("year range" in error for error in result.errors)


def test_no_trade_day_has_no_range_and_carries_the_close_forward(make_tick):
    # No trades yet: GSE carries the previous close forward as the
    # closing price, and there is no open or day range to check it by.
    tick = make_tick(
        price=6.50, last_trade_price=6.61, previous_close=6.50,
        open=None, day_high=None, day_low=None,
        shares_traded=0, value_traded=0,
    )

    assert validate_tick(tick).is_valid
    assert (tick.change, tick.change_percent) == (0, 0)


def test_traded_day_without_a_range_is_rejected(make_tick):
    tick = make_tick(day_high=None, day_low=None, shares_traded=100)

    result = validate_tick(tick)

    assert not result.is_valid
    assert any("day range is missing" in error for error in result.errors)


def test_change_is_vwap_against_previous_vwap_not_last_trade(make_tick):
    tick = make_tick(price=6.54, last_trade_price=6.58, previous_close=6.50)

    assert tick.change == 0.04
    assert tick.change_percent == 0.62
    dumped = tick.model_dump(mode="json")
    assert (dumped["change"], dumped["change_percent"]) == (0.04, 0.62)


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
    good_1 = make_tick(price=6.54)
    bad = make_tick(bid=6.60, ask=6.55)
    good_2 = make_tick(price=6.55)

    async def feed():
        for tick in (good_1, bad, good_2):
            yield tick

    stream = ValidatingStream(feed())
    seen = [tick async for tick in stream]

    assert seen == [good_1, good_2]
