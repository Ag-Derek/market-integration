"""
Several series on one chart, and how they move together: the maths
behind /graph (Bloomberg's G) and its correlation matrix (CORR). Pure
functions over dated series, so each can be tested on hand-made data;
app/main.py turns candles, index levels and FX fixes into them.

A series is a list of Points, oldest first, at most one per day. A point
is `observed` when something printed that day: an equity or bond that
traded, an index level, a Bank of Ghana fix. A day a thin stock didn't
trade still has a candle (its last close, carried), but no one dealt at
that price, so it is not observed.

**Rebasing.** Each series is divided by its value at the start of the
range and multiplied by 100, so a stock at GH₵ 0.40 and the index at
4,000 can share an axis: 112 means up 12% since the start. The base is
the last value on or before the start (the start may fall on a weekend,
or a day a stock didn't trade); a series that starts later (listed in
the range) is based on its first value, and says so.

**Correlation, and thin trading.** Correlation is of changes, never of
levels (any two trending series look correlated): the relative return
of prices, index levels and FX rates, and the change in percentage
points of a yield. Changes are measured *between days both series were
observed*, per pair: if stock A trades Monday and Thursday and B every
day, the pair uses A's Monday-to-Thursday return against B's over the
same three days. Measuring each on calendar days instead would give A a
run of zero returns followed by the whole move on Thursday, while B's
move is spread across the week; the two then look less related than
they are (stale prices bias correlation towards zero). The cost is
fewer observations for thin names, so each cell reports how many it
rests on, and is null below MIN_OBSERVATIONS. Weekly frequency (each
series' last observation of the week) is the other cure, offered as an
option: fewer, but fuller, observations.
"""

import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal, Optional, Sequence

Change = Literal["return", "difference"]
Frequency = Literal["daily", "weekly"]

MAX_SERIES = 8   # the chart's categorical palette has eight slots
MIN_OBSERVATIONS = 20


@dataclass(frozen=True)
class Point:
    on: date
    value: float
    observed: bool = True


@dataclass(frozen=True)
class GraphRange:
    lookback: Optional[timedelta]   # None = everything (Max) or YTD
    year_to_date: bool = False
    weekly: bool = False            # chart points thinned to one a week

    def start(self, today: date) -> Optional[date]:
        if self.year_to_date:
            return date(today.year, 1, 1)
        if self.lookback is None:
            return None
        return today - self.lookback


# Daily history throughout; the long ranges draw one point a week, so
# eight 10-year series stay a light chart. Correlation always uses every
# observation.
GRAPH_RANGES: dict[str, GraphRange] = {
    "1M": GraphRange(timedelta(days=30)),
    "3M": GraphRange(timedelta(days=91)),
    "6M": GraphRange(timedelta(days=182)),
    "YTD": GraphRange(None, year_to_date=True),
    "1Y": GraphRange(timedelta(days=365)),
    "2Y": GraphRange(timedelta(days=730)),
    "5Y": GraphRange(timedelta(days=5 * 365 + 1), weekly=True),
    "Max": GraphRange(None, weekly=True),
}
DEFAULT_RANGE = "1Y"


# ---------------------------------------------------------------- rebasing

def rebase(points: Sequence[Point], start: Optional[date]) -> Optional[dict]:
    """The series from the range's start, each value with its level
    rebased to 100 at the base: {base, late_start, points: [[iso date,
    value, rebased], ...]}. The base is the last point on or before
    `start`, else (a series that begins inside the range) its first
    point, flagged `late_start`. None for an empty series. A base of 0
    or less can't be rebased: every rebased value is then None."""
    if not points:
        return None
    before = [p for p in points if start is not None and p.on <= start]
    base = before[-1] if before else points[0]
    shown = [p for p in points if p.on >= base.on]
    scale = 100 / base.value if base.value > 0 else None
    return {
        "base": {"date": base.on.isoformat(), "value": base.value},
        "late_start": start is not None and base.on > start,
        "points": [
            [p.on.isoformat(), p.value, None if scale is None else round(p.value * scale, 4)]
            for p in shown
        ],
    }


def weekly(rows: Sequence[list]) -> list[list]:
    """Rebased rows thinned to the last of each ISO week, keeping the
    first row (the base) so the line still starts at 100."""
    out: list[list] = []
    week = None
    for i, row in enumerate(rows):
        key = date.fromisoformat(row[0]).isocalendar()[:2]
        if i > 0 and key == week and len(out) > 1:
            out[-1] = row
        else:
            out.append(row)
        week = key
    return out


# ---------------------------------------------------------------- correlation

def observations(
    points: Sequence[Point], start: Optional[date], frequency: Frequency = "daily",
) -> dict:
    """The series' observed values from `start` on, keyed by day (or by
    ISO (year, week) for weekly: the week's last observation)."""
    out: dict = {}
    for p in points:
        if not p.observed or (start is not None and p.on < start):
            continue
        key = p.on if frequency == "daily" else p.on.isocalendar()[:2]
        out[key] = p.value
    return out


def change(previous: float, current: float, kind: Change) -> Optional[float]:
    if kind == "difference":
        return current - previous
    return current / previous - 1 if previous > 0 else None


def own_changes(obs: dict, kind: Change) -> list[float]:
    keys = sorted(obs)
    out = [change(obs[a], obs[b], kind) for a, b in zip(keys, keys[1:])]
    return [c for c in out if c is not None]


def paired_changes(a: dict, kind_a: Change, b: dict, kind_b: Change) -> list[tuple[float, float]]:
    """Each series' change between consecutive keys both were observed
    on: the same span of time for both, however thinly either trades."""
    common = sorted(set(a) & set(b))
    pairs = []
    for k0, k1 in zip(common, common[1:]):
        ca, cb = change(a[k0], a[k1], kind_a), change(b[k0], b[k1], kind_b)
        if ca is not None and cb is not None:
            pairs.append((ca, cb))
    return pairs


def pearson(pairs: Sequence[tuple[float, float]]) -> Optional[float]:
    """Pearson correlation, or None with fewer than MIN_OBSERVATIONS
    pairs or a side that never moved."""
    n = len(pairs)
    if n < MIN_OBSERVATIONS:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    return max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy)))


def correlation_matrix(
    series: Sequence[tuple[Sequence[Point], Change]],
    start: Optional[date],
    frequency: Frequency = "daily",
) -> list[list[dict]]:
    """matrix[i][j] = {"value", "observations"} for every pair of
    `series` (each its points and how it changes), symmetric. The
    diagonal is 1 for a series with enough changes of its own (its
    observation count either way). `value` is None, with the count, when
    a pair has fewer than MIN_OBSERVATIONS common changes."""
    obs = [observations(points, start, frequency) for points, _ in series]
    kinds = [kind for _, kind in series]
    n = len(series)
    matrix: list[list[dict]] = [[{} for _ in range(n)] for _ in range(n)]
    for i in range(n):
        own = own_changes(obs[i], kinds[i])
        moved = len(own) >= MIN_OBSERVATIONS and len(set(own)) > 1
        matrix[i][i] = {"value": 1.0 if moved else None, "observations": len(own)}
        for j in range(i + 1, n):
            pairs = paired_changes(obs[i], kinds[i], obs[j], kinds[j])
            r = pearson(pairs)
            cell = {"value": None if r is None else round(r, 4), "observations": len(pairs)}
            matrix[i][j] = matrix[j][i] = cell
    return matrix
