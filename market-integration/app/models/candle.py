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
from typing import Iterable

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
    out: list[Candle] = []
    for c in candles:
        start = bucket_start(c.window_start, interval)
        if out and out[-1].window_start == start:
            last = out[-1]
            last.high = max(last.high, c.high)
            last.low = min(last.low, c.low)
            last.close = c.close
            last.volume += c.volume
            last.tick_count += c.tick_count
        else:
            out.append(Candle(
                symbol=c.symbol,
                interval=interval,
                window_start=start,
                window_end=bucket_end(start, interval),
                open=c.open,
                high=c.high,
                low=c.low,
                close=c.close,
                volume=c.volume,
                tick_count=c.tick_count,
            ))
    return out
