"""
Seasonality (Bloomberg's SEAG) from stored daily candles: how a stock
has tended to do in each calendar month, and each year's path from
1 January. Pure functions over a symbol's 1d candles reduced to Days
(performance.days()), so they can be tested on hand-made series.

A month's return is its last close against the previous month's last
close; a year's, its last close against the previous year's. A month
with no previous month in the series (the first one, or after a gap)
has no return, rather than one measured from somewhere mid-month. The
current month and year are month- and year-to-date: shown, flagged
`partial`, and left out of the averages, so an unfinished month never
counts as a finished one.

Returns are in percent, in the candles' currency (cedis).
"""

from datetime import date
from statistics import mean, median
from typing import Optional, Sequence

from app.company.performance import Day


def _month_ends(series: Sequence[Day]) -> dict[tuple[int, int], Day]:
    """(year, month) -> that month's last day in the series."""
    out: dict[tuple[int, int], Day] = {}
    for d in series:
        out[(d.on.year, d.on.month)] = d
    return out


def _previous(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _pct(base: float, value: float) -> Optional[float]:
    return round((value / base - 1) * 100, 4) if base > 0 else None


def monthly_returns(series: Sequence[Day], today: date) -> list[dict]:
    """Every month in the series, oldest first: {year, month, return,
    partial}. `return` is None without the previous month's close."""
    ends = _month_ends(series)
    out = []
    for (year, month), end in sorted(ends.items()):
        base = ends.get(_previous(year, month))
        out.append({
            "year": year,
            "month": month,
            "return": _pct(base.close, end.close) if base else None,
            "partial": (year, month) == (today.year, today.month),
        })
    return out


def annual_returns(series: Sequence[Day], today: date) -> list[dict]:
    """Every year in the series: {year, return, partial}. `partial` for
    the current year (to date); `return` None without the previous
    year's December close."""
    ends = _month_ends(series)
    years = sorted({y for y, _ in ends})
    out = []
    for year in years:
        last = max((d for (y, _), d in ends.items() if y == year), key=lambda d: d.on)
        base = ends.get((year - 1, 12))
        out.append({
            "year": year,
            "return": _pct(base.close, last.close) if base else None,
            "partial": year == today.year,
        })
    return out


def by_month(months: Sequence[dict]) -> list[dict]:
    """For each calendar month, over its finished months with a return:
    mean and median return, how many were positive, and the best and
    worst year. Every month is listed; one with no observations has
    count 0 and nulls."""
    out = []
    for m in range(1, 13):
        obs = [r for r in months if r["month"] == m and r["return"] is not None and not r["partial"]]
        values = [r["return"] for r in obs]
        best = max(obs, key=lambda r: r["return"]) if obs else None
        worst = min(obs, key=lambda r: r["return"]) if obs else None
        out.append({
            "month": m,
            "count": len(obs),
            "positive": sum(1 for v in values if v > 0),
            "mean": round(mean(values), 4) if values else None,
            "median": round(median(values), 4) if values else None,
            "best": {"year": best["year"], "return": best["return"]} if best else None,
            "worst": {"year": worst["year"], "return": worst["return"]} if worst else None,
        })
    return out


def year_paths(series: Sequence[Day]) -> list[dict]:
    """Each year's cumulative return from the previous year's last
    close, as [day of year, percent] points, oldest year first. The
    first year in the series has no such close, so it is measured from
    its own first close instead, and flagged `partial_start`."""
    ends = _month_ends(series)
    by_year: dict[int, list[Day]] = {}
    for d in series:
        by_year.setdefault(d.on.year, []).append(d)
    out = []
    for year in sorted(by_year):
        days = by_year[year]
        prior = ends.get((year - 1, 12))
        base = prior.close if prior else days[0].close
        if base <= 0:
            continue
        out.append({
            "year": year,
            "partial_start": prior is None,
            "points": [[d.on.timetuple().tm_yday, round((d.close / base - 1) * 100, 4)] for d in days],
        })
    return out


def average_path(paths: Sequence[dict], today: date) -> tuple[list[list[float]], int]:
    """The mean of the finished years' paths that start from a previous
    year's close, at every day of the year (each year carrying its last
    value forward over days it has none, such as weekends). Returns
    (points, how many years went into it)."""
    full = [p for p in paths if not p["partial_start"] and p["year"] < today.year]
    if not full:
        return [], 0
    filled = []
    for p in full:
        by_day = dict((int(day), v) for day, v in p["points"])
        last, row = 0.0, []
        for day in range(1, 367):
            last = by_day.get(day, last)
            row.append(last)
        filled.append(row)
    # To the last day any of them reaches: 365, or 366 with a leap year.
    end = max(int(p["points"][-1][0]) for p in full)
    points = [[day, round(mean(r[day - 1] for r in filled), 4)] for day in range(1, end + 1)]
    return points, len(full)


def seasonality(series: Sequence[Day], today: date) -> dict:
    """Everything the stock page's Seasonality view draws."""
    months = monthly_returns(series, today)
    paths = year_paths(series)
    average, average_years = average_path(paths, today)
    return {
        "from": series[0].on.isoformat() if series else None,
        "to": series[-1].on.isoformat() if series else None,
        "months": months,
        "annual": annual_returns(series, today),
        "by_month": by_month(months),
        "paths": paths,
        "average": average,
        "average_years": average_years,
    }
