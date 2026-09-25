"""
Business-rule validation for market data ticks.

Pydantic's `MarketData` model already enforces structural rules (types,
positive prices, non-negative volumes, ...) at construction time -- a
tick that violates those never even becomes a MarketData object. This
module adds the cross-field business rules the schema can't express on
its own: relationships between fields (bid < ask, price within the
day's range) and freshness (the tick isn't stale or timestamped in the
future).

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

from app.models.candle import INTERVALS, Candle, bucket_end, bucket_start
from app.models.market_data import MarketData

logger = logging.getLogger(__name__)

# How far a tick's timestamp may drift from "now" before we stop
# trusting it as a live tick (clock skew, a stalled pipeline, etc.).
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


def validate_tick(tick: MarketData, *, now: datetime | None = None) -> ValidationResult:
    """Run business-rule checks on an already schema-valid MarketData tick."""
    now = now or datetime.now(timezone.utc)
    errors: list[str] = []

    if tick.bid >= tick.ask:
        errors.append(f"bid ({tick.bid}) is not less than ask ({tick.ask})")

    if tick.day_low > tick.day_high:
        errors.append(f"day_low ({tick.day_low}) > day_high ({tick.day_high})")
    elif not (tick.day_low <= tick.price <= tick.day_high):
        errors.append(
            f"price ({tick.price}) outside day range [{tick.day_low}, {tick.day_high}]"
        )

    if not (tick.week52_low <= tick.price <= tick.week52_high):
        errors.append(
            f"price ({tick.price}) outside 52-week range "
            f"[{tick.week52_low}, {tick.week52_high}]"
        )

    age = now - tick.timestamp
    if age > MAX_TICK_AGE:
        errors.append(f"tick is stale: {age.total_seconds():.1f}s old")
    elif age < -MAX_CLOCK_SKEW_AHEAD:
        errors.append(f"tick is timestamped {(-age).total_seconds():.1f}s in the future")

    return ValidationResult(is_valid=not errors, errors=errors)


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

    def __init__(self, feed: AsyncIterator[MarketData], name: str = "validator"):
        self._feed = feed
        self._name = name

    async def __aiter__(self) -> AsyncIterator[MarketData]:
        async for tick in self._feed:
            result = validate_tick(tick)
            if result:
                yield tick
            else:
                logger.warning(
                    "[%s] rejected tick for %s: %s",
                    self._name, tick.symbol, "; ".join(result.errors),
                )
