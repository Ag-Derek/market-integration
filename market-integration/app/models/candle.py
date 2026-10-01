"""
Canonical OHLCV candle shape, plus the time grid candles are bucketed on.

Both producers of candles -- a connector's historical backfill and the
live MarketAggregator -- bucket time with bucket_start() below, so a
backfilled candle and a live one for the same window always share the
same window_start and can overwrite/extend each other cleanly.

Buckets are aligned to the UTC epoch (so "1d" is UTC midnight to
midnight), except "1w", which starts on Monday 00:00 UTC.
"""

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from pydantic import BaseModel

INTERVALS: dict[str, timedelta] = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "1d": timedelta(days=1),
    "1w": timedelta(weeks=1),
}

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# 1970-01-05 was a Monday; weeks are aligned to it rather than to the
# epoch itself (a Thursday).
_WEEK_ANCHOR = datetime(1970, 1, 5, tzinfo=timezone.utc)


class Candle(BaseModel):
    symbol: str
    interval: str
    window_start: datetime
    window_end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    # How many live ticks were folded into this candle. 0 for candles
    # that came from a provider's history (backfill) rather than ticks.
    tick_count: int = 0
    # Fixed income only: the same window in yield (% a year), so a chart
    # can toggle between price and yield. OHLC above is then clean price.
    # Its own range, not derived from price's: the high yield is the low
    # price. None for equities and for price-only (corporate) bonds.
    yield_open: Optional[float] = None
    yield_high: Optional[float] = None
    yield_low: Optional[float] = None
    yield_close: Optional[float] = None
    # True when the live feed lost a tick that belonged to this window
    # (backpressure on the aggregator's buffer queue), so its high, low
    # or volume may be off. Never set on provider history.
    possibly_incomplete: bool = False


def bucket_start(ts: datetime, interval: str) -> datetime:
    """Start of the `interval` bucket containing `ts` (UTC)."""
    step = INTERVALS[interval]
    anchor = _WEEK_ANCHOR if interval == "1w" else _EPOCH
    ts = ts.astimezone(timezone.utc)
    return anchor + ((ts - anchor) // step) * step


def bucket_end(start: datetime, interval: str) -> datetime:
    return start + INTERVALS[interval]


def resample(candles: Iterable[Candle], interval: str) -> list[Candle]:
    """Roll finer, time-ordered candles up into coarser `interval` ones."""
    # Accumulates each bucket in a plain list and builds its Candle once:
    # mutating pydantic models field by field is slow at the scale of a
    # startup backfill (hundreds of thousands of input candles).
    out: list[Candle] = []
    symbol = None
    acc = None  # [start, open, high, low, close, volume, tick_count, possibly_incomplete]
    yld = None  # [open, high, low, close], or None while no input had a yield

    def emit() -> None:
        start, open_, high, low, close, volume, tick_count, possibly_incomplete = acc
        yield_open, yield_high, yield_low, yield_close = yld or (None,) * 4
        out.append(Candle(
            symbol=symbol,
            interval=interval,
            window_start=start,
            window_end=bucket_end(start, interval),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            tick_count=tick_count,
            yield_open=yield_open,
            yield_high=yield_high,
            yield_low=yield_low,
            yield_close=yield_close,
            possibly_incomplete=possibly_incomplete,
        ))

    for c in candles:
        start = bucket_start(c.window_start, interval)
        if acc is not None and acc[0] == start:
            if c.high > acc[2]:
                acc[2] = c.high
            if c.low < acc[3]:
                acc[3] = c.low
            acc[4] = c.close
            acc[5] += c.volume
            acc[6] += c.tick_count
            acc[7] = acc[7] or c.possibly_incomplete
        else:
            if acc is not None:
                emit()
            symbol = c.symbol
            acc = [start, c.open, c.high, c.low, c.close, c.volume, c.tick_count, c.possibly_incomplete]
            yld = None
        if c.yield_close is not None:
            if yld is None:
                yld = [c.yield_open, c.yield_high, c.yield_low, c.yield_close]
            else:
                yld[1] = max(yld[1], c.yield_high)
                yld[2] = min(yld[2], c.yield_low)
                yld[3] = c.yield_close
    if acc is not None:
        emit()
    return out
