"""
A simulated GSE Composite Index (GSE-CI), for beta until the feed
carries the real one (#62).

Built from the equities' daily candles: each day, the average of every
constituent's close-to-close return (equal-weighted), chained from 1,000
on the first day. A constituent with no candle for a day is carried at
its last close (no return). The real GSE-CI is weighted by market
capitalisation, which needs shares outstanding we don't have for most
companies yet, so this is a stand-in with the right shape, not the
published figure. Replace it with the index from the feed when there is
one.
"""

from datetime import date
from typing import Mapping, Sequence

from app.company.performance import Day

BASE_LEVEL = 1000.0


def simulated_gse_ci(constituents: Mapping[str, Sequence[Day]]) -> list[Day]:
    """Daily index levels, oldest first, one per day any constituent has
    a candle. Days carry no high/low or volume of their own: high = low =
    close, volume 0."""
    closes: dict[str, dict[date, float]] = {
        symbol: {d.on: d.close for d in series if d.close > 0} for symbol, series in constituents.items()
    }
    calendar = sorted({on for series in closes.values() for on in series})
    level = BASE_LEVEL
    last: dict[str, float] = {}
    out: list[Day] = []
    for on in calendar:
        returns = []
        for symbol, series in closes.items():
            previous = last.get(symbol)
            close = series.get(on)
            if close is None:
                if previous is not None:
                    returns.append(0.0)  # no candle that day: carried, unchanged
                continue
            if previous is not None:
                returns.append(close / previous - 1)
            last[symbol] = close
        if returns and out:
            level *= 1 + sum(returns) / len(returns)
        out.append(Day(on, round(level, 4), round(level, 4), round(level, 4), 0))
    return out
