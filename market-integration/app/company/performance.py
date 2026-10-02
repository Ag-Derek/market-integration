"""
Price performance from stored daily candles (#62): 1-day, 52-week, YTD
and 12-month total return, and beta against an index. Pure functions
over a symbol's 1d candles (oldest first, today's still-open candle
last), so each can be tested on hand-made series.

Prices here are candle closes: the last trade of each day, carried
over on a day with no trades. That is the dated history we store; the
GSE's official closing price (session VWAP) is used for valuation
instead (figures.py). A day missing from the series (a real feed's
holiday or outage) is skipped, not invented: references look up the
last close on or before the date they need.

Every function returns None, rather than a misleading number, when the
history doesn't cover what it needs.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional, Sequence

from app.models.candle import Candle


@dataclass(frozen=True)
class Day:
    """One daily candle, reduced to what the figures use."""
    on: date
    close: float
    high: float
    low: float
    volume: int


def days(candles: Iterable[Candle]) -> list[Day]:
    out = [Day(c.window_start.date(), c.close, c.high, c.low, c.volume) for c in candles]
    return sorted(out, key=lambda d: d.on)


def close_on_or_before(series: Sequence[Day], on: date) -> Optional[Day]:
    best = None
    for d in series:
        if d.on > on:
            break
        best = d
    return best


def _change(base: Optional[Day], latest: Optional[Day]) -> Optional[dict]:
    if base is None or latest is None or base.on >= latest.on or base.close <= 0:
        return None
    change = latest.close - base.close
    return {
        "value": round(change, 4),
        "percent": round(change / base.close * 100, 4),
        "from": base.on.isoformat(),
        "to": latest.on.isoformat(),
        "from_price": base.close,
        "to_price": latest.close,
    }


def one_day_change(series: Sequence[Day]) -> Optional[dict]:
    """Latest close against the previous day's in the series."""
    if len(series) < 2:
        return None
    return _change(series[-2], series[-1])


def ytd_change(series: Sequence[Day], today: date) -> Optional[dict]:
    """Latest close against the last close of the previous year. None if
    the history doesn't reach back past 1 January (listed this year)."""
    if not series:
        return None
    return _change(close_on_or_before(series, date(today.year - 1, 12, 31)), series[-1])


def week52_range(series: Sequence[Day], today: date) -> Optional[dict]:
    """Highest high and lowest low over the last 52 weeks, each with its
    date. Only days with trades count: a no-trade day's flat candle is
    the carried-over price, not a price anyone dealt at. None if nothing
    traded in the period."""
    since = today - timedelta(days=364)
    traded = [d for d in series if d.on >= since and d.volume > 0]
    if not traded:
        return None
    high = max(traded, key=lambda d: (d.high, d.on))   # the latest day, on a tie
    low = min(traded, key=lambda d: (d.low, -d.on.toordinal()))
    return {
        "high": high.high, "high_date": high.on.isoformat(),
        "low": low.low, "low_date": low.on.isoformat(),
        "from": since.isoformat(), "trading_days": len(traded),
    }


def total_return_12m(
    series: Sequence[Day], today: date, dividends: Iterable[tuple[date, float]],
) -> Optional[dict]:
    """Price change plus dividends paid over the last 12 months, against
    the close 12 months ago. `dividends` are (payment date, amount per
    share) in the price's currency; those paid after the base date and
    up to today count."""
    if not series:
        return None
    base = close_on_or_before(series, today - timedelta(days=365))
    change = _change(base, series[-1])
    if change is None:
        return None
    paid = [(on, amount) for on, amount in dividends if base.on < on <= today]
    dividend_total = sum(amount for _, amount in paid)
    return {
        **change,
        "price_return_percent": change["percent"],
        "dividends": round(dividend_total, 4),
        "dividends_paid": [{"date": on.isoformat(), "amount": amount} for on, amount in sorted(paid)],
        "total_return_percent": round((change["value"] + dividend_total) / base.close * 100, 4),
    }


def daily_returns(series: Sequence[Day]) -> dict[date, float]:
    """Close-to-close return for each day that has a previous day in the
    series."""
    return {
        cur.on: cur.close / prev.close - 1
        for prev, cur in zip(series, series[1:])
        if prev.close > 0
    }


MIN_BETA_OBSERVATIONS = 30


def beta(stock: Sequence[Day], index: Sequence[Day], today: date) -> Optional[dict]:
    """Slope of the stock's daily returns on the index's over the last
    year: cov(stock, index) / var(index), on the days both have a return.
    None with fewer than MIN_BETA_OBSERVATIONS days, or an index that
    didn't move."""
    since = today - timedelta(days=365)
    s, i = daily_returns(stock), daily_returns(index)
    pairs = [(s[d], i[d]) for d in sorted(set(s) & set(i)) if d > since]
    if len(pairs) < MIN_BETA_OBSERVATIONS:
        return None
    n = len(pairs)
    mean_s = sum(p[0] for p in pairs) / n
    mean_i = sum(p[1] for p in pairs) / n
    var_i = sum((p[1] - mean_i) ** 2 for p in pairs) / (n - 1)
    if var_i == 0:
        return None
    cov = sum((p[0] - mean_s) * (p[1] - mean_i) for p in pairs) / (n - 1)
    return {"value": round(cov / var_i, 4), "observations": n, "from": since.isoformat()}
