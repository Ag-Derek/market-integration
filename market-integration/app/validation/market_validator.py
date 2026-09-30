"""
Business-rule validation for market data ticks.

Pydantic's `MarketData` model already enforces structural rules (types,
positive prices, non-negative volumes, ...) at construction time -- a
tick that violates those never even becomes a MarketData object. This
module adds the cross-field business rules the schema can't express on
its own: relationships between fields (bid < ask when both sides are
quoted, price within the day's and year's range) and freshness (the
feed published the tick recently and not in the future; how long ago the
symbol last traded doesn't matter).

Fixed-income ticks keep their schema permissive and get their rules
here instead: prices positive, yields within configurable bounds
(config.FI_YIELD_MIN/MAX), the security not yet matured, and a bid
yield no lower than the ask. Repo (sell/buy-back) ticks get the same
price, yield and maturity checks, and a bond yield equal to the bond
price is rejected as a price in the yield field (GFIM quirk 12).

`validate_tick` is a plain, dependency-free function on purpose: it is
called independently by more than one downstream branch off the buffer
(a real-time ValidatingStream here, and the aggregator's own filtering
in app.aggregation.market_aggregator). Neither branch depends on the
other, so a slow/backed-up database write can never delay real-time
delivery, and vice versa.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from app import config
from app.models.candle import INTERVALS, Candle, bucket_end, bucket_start
from app.models.fixed_income import FixedIncomeTick, RepoTick
from app.models.market_data import MarketData
from app.models.tick import Tick

logger = logging.getLogger(__name__)

# How far a tick's publish timestamp may drift from "now" before we stop
# trusting it as a live tick (clock skew, a stalled pipeline, etc.). This
# is about the feed, not the market: last_trade_at has no age limit.
MAX_TICK_AGE = timedelta(seconds=30)
MAX_CLOCK_SKEW_AHEAD = timedelta(seconds=5)


class ValidationResult:
    __slots__ = ("is_valid", "errors")

    def __init__(self, is_valid: bool, errors: list[str]):
        self.is_valid = is_valid
        self.errors = errors

    def __bool__(self) -> bool:
        return self.is_valid

    def __repr__(self) -> str:
        return f"ValidationResult(is_valid={self.is_valid}, errors={self.errors})"


def validate_tick(
    tick: Tick,
    *,
    now: datetime | None = None,
    yield_bounds: tuple[float, float] | None = None,
) -> ValidationResult:
    """Run business-rule checks on an already schema-valid tick of any
    kind. `yield_bounds` (min, max) applies to fixed income and defaults
    to config.FI_YIELD_MIN/FI_YIELD_MAX."""
    now = now or datetime.now(timezone.utc)
    if isinstance(tick, MarketData):
        errors = _equity_errors(tick)
    else:
        bounds = yield_bounds or (config.FI_YIELD_MIN, config.FI_YIELD_MAX)
        if isinstance(tick, FixedIncomeTick):
            errors = _fixed_income_errors(tick, now, bounds)
        else:
            errors = _repo_errors(tick, now, bounds)
    errors += _clock_errors(tick, now)
    return ValidationResult(is_valid=not errors, errors=errors)


def _equity_errors(tick: MarketData) -> list[str]:
    errors: list[str] = []

    # A one-sided or empty book is normal on the GSE; only a crossed or
    # locked two-sided book is an error.
    if tick.bid is not None and tick.ask is not None and tick.bid >= tick.ask:
        errors.append(f"bid ({tick.bid}) is not less than ask ({tick.ask})")

    if tick.day_low > tick.day_high:
        errors.append(f"day_low ({tick.day_low}) > day_high ({tick.day_high})")
    else:
        # The session VWAP is only made of today's trades once there are
        # some; before that it is the previous session's, carried over.
        fields = ("price", "vwap") if tick.volume > 0 else ("price",)
        for field in fields:
            value = getattr(tick, field)
            if not (tick.day_low <= value <= tick.day_high):
                errors.append(
                    f"{field} ({value}) outside day range "
                    f"[{tick.day_low}, {tick.day_high}]"
                )

    for label, low, high in (
        ("year", tick.year_low, tick.year_high),
        ("52-week", tick.week52_low, tick.week52_high),
    ):
        if not (low <= tick.price <= high):
            errors.append(f"price ({tick.price}) outside {label} range [{low}, {high}]")

    return errors


_FI_PRICES = ("bid_price", "ask_price", "opening_price", "closing_price", "day_low_price", "day_high_price")
_FI_YIELDS = ("bid_yield", "ask_yield", "opening_yield", "closing_yield", "day_low_yield", "day_high_yield")


def _fixed_income_errors(
    tick: FixedIncomeTick, now: datetime, bounds: tuple[float, float]
) -> list[str]:
    # No day-range checks: GFIM closes routinely fall outside the traded
    # range, and price ranges come reversed (docs/data-formats.md,
    # quirks 7 and 9).
    errors = _positive(tick, _FI_PRICES) + _in_bounds(tick, _FI_YIELDS, bounds)
    errors += _maturity_errors(tick, now)

    # Quoted by yield, a bid is the *higher* yield (the lower price).
    # Unlike equities, a locked book (bid = ask) is allowed.
    if tick.bid_yield is not None and tick.ask_yield is not None and tick.bid_yield < tick.ask_yield:
        errors.append(f"bid_yield ({tick.bid_yield}) is below ask_yield ({tick.ask_yield})")
    if tick.bid_price is not None and tick.ask_price is not None and tick.bid_price > tick.ask_price:
        errors.append(f"bid_price ({tick.bid_price}) is above ask_price ({tick.ask_price})")
    return errors


def _repo_errors(tick: RepoTick, now: datetime, bounds: tuple[float, float]) -> list[str]:
    errors = (
        _positive(tick, ("bond_price",))
        + _in_bounds(tick, ("bond_yield", "repo_rate"), bounds)
        + _maturity_errors(tick, now)
    )
    # A yield equal to the price is the report's price-in-the-yield-column
    # error (quirk 12), which the bounds alone can't catch (79.96 < 100).
    if tick.bond_yield is not None and tick.bond_yield == tick.bond_price:
        errors.append(f"bond_yield ({tick.bond_yield}) equals bond_price: a price in the yield field")
    return errors


def _positive(tick, fields: tuple[str, ...]) -> list[str]:
    return [
        f"{f} ({v}) is not positive"
        for f in fields
        if (v := getattr(tick, f)) is not None and v <= 0
    ]


def _in_bounds(tick, fields: tuple[str, ...], bounds: tuple[float, float]) -> list[str]:
    low, high = bounds
    return [
        f"{f} ({v}) outside [{low}, {high}]"
        for f in fields
        if (v := getattr(tick, f)) is not None and not (low <= v <= high)
    ]


def _maturity_errors(tick: FixedIncomeTick | RepoTick, now: datetime) -> list[str]:
    # A security redeemed today no longer trades.
    if tick.maturity_date <= now.date():
        return [f"security has matured (maturity_date {tick.maturity_date.isoformat()})"]
    return []


def _clock_errors(tick: Tick, now: datetime) -> list[str]:
    errors: list[str] = []
    # Freshness is judged on when the feed published the quote, never on
    # the last trade: that can legitimately be hours old (see MarketData).
    age = now - tick.timestamp
    if age > MAX_TICK_AGE:
        errors.append(f"tick is stale: published {age.total_seconds():.1f}s ago")
    elif age < -MAX_CLOCK_SKEW_AHEAD:
        errors.append(f"tick is timestamped {(-age).total_seconds():.1f}s in the future")

    if tick.last_trade_at is not None:
        if tick.last_trade_at - tick.timestamp > MAX_CLOCK_SKEW_AHEAD:
            errors.append(
                f"last_trade_at ({tick.last_trade_at.isoformat()}) is after the quote's "
                f"timestamp ({tick.timestamp.isoformat()})"
            )
    elif tick.volume > 0:
        errors.append(f"volume ({tick.volume}) traded this session but no last_trade_at")

    return errors


def validate_candle(candle: Candle) -> ValidationResult:
    """OHLCV invariants every candle must satisfy, whether it was built
    from live ticks or fetched from a provider's history."""
    errors: list[str] = []

    if candle.interval not in INTERVALS:
        errors.append(f"unknown interval {candle.interval!r}")
    else:
        if candle.window_start != bucket_start(candle.window_start, candle.interval):
            errors.append(
                f"window_start ({candle.window_start.isoformat()}) is not aligned "
                f"to the {candle.interval} grid"
            )
        if candle.window_end != bucket_end(candle.window_start, candle.interval):
            errors.append(
                f"window_end ({candle.window_end.isoformat()}) is not "
                f"window_start + {candle.interval}"
            )

    if min(candle.open, candle.high, candle.low, candle.close) <= 0:
        errors.append("prices must be positive")
    if candle.low > candle.high:
        errors.append(f"low ({candle.low}) > high ({candle.high})")
    else:
        if not (candle.low <= candle.open <= candle.high):
            errors.append(f"open ({candle.open}) outside [{candle.low}, {candle.high}]")
        if not (candle.low <= candle.close <= candle.high):
            errors.append(f"close ({candle.close}) outside [{candle.low}, {candle.high}]")

    # Yield OHLC is all-or-nothing, and a range of its own: the high
    # yield is not the high price's yield.
    yields = (candle.yield_open, candle.yield_high, candle.yield_low, candle.yield_close)
    if any(y is not None for y in yields):
        if any(y is None for y in yields):
            errors.append("yield open/high/low/close must be all set or all empty")
        elif candle.yield_low > candle.yield_high:
            errors.append(f"yield_low ({candle.yield_low}) > yield_high ({candle.yield_high})")
        else:
            for label, y in (("yield_open", candle.yield_open), ("yield_close", candle.yield_close)):
                if not (candle.yield_low <= y <= candle.yield_high):
                    errors.append(f"{label} ({y}) outside [{candle.yield_low}, {candle.yield_high}]")

    if candle.volume < 0:
        errors.append(f"volume ({candle.volume}) is negative")
    if candle.tick_count < 0:
        errors.append(f"tick_count ({candle.tick_count}) is negative")

    return ValidationResult(is_valid=not errors, errors=errors)


class ValidatingStream:
    """Wraps a raw tick feed (e.g. buffer.subscribe_latest("validator"))
    and yields only ticks that pass validate_tick(). Invalid ticks are
    logged and dropped here, so real-time consumers (Symphony, alerts,
    a live gateway, ...) can read from this instead of the raw buffer
    feed and never see a bad tick.

    This is intentionally a separate branch off the buffer, not a gate
    in front of the aggregator -- a real-time consumer should never
    have to wait behind the aggregator's database flush cycle.
    """

    def __init__(self, feed: AsyncIterator[Tick], name: str = "validator"):
        self._feed = feed
        self._name = name

    async def __aiter__(self) -> AsyncIterator[Tick]:
        async for tick in self._feed:
            result = validate_tick(tick)
            if result:
                yield tick
            else:
                logger.warning(
                    "[%s] rejected %s tick for %s: %s",
                    self._name, tick.tick_type, tick.symbol, "; ".join(result.errors),
                )
