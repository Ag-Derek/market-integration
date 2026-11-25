"""
The stock screener (#99; Bloomberg's EQS): filter and sort GSE companies
by the ratios app/company/figures.py already calculates for each
description page, so a ratio here is always the one its page shows.

The universe is every company with a file in data/companies/. Filters
are a sector and bounds on the figures (FILTERS); with none set, every
company appears. A company without the filtered figure (a loss-maker's
P/E, a dividend yield nobody has recorded, a ratio not taken because
the company reports in another currency) is left out and counted, with
the figure's reason, never treated as zero. Sorting never drops a row:
companies without the sort figure go last. See docs/screener.md.
"""

from dataclasses import dataclass
from typing import Iterable, Optional

from app.models.screener import (
    ScreenerColumn,
    ScreenerFigure,
    ScreenerFilter,
    ScreenerResult,
    ScreenerRow,
    ScreenerSort,
)

CURRENCY = "GHS"

# Shown as columns, in order: (figure, label, unit, formula). The
# formulas are figures.py's; each row's figure carries its own too.
COLUMNS = (
    ("price", "Price", "GHS", "session VWAP (GSE closing price); latest daily close with no live quote"),
    ("market_cap", "Market cap", "GHS", "price x shares outstanding"),
    ("pe_ratio", "P/E", "x", "price / trailing EPS"),
    ("price_to_book", "P/B", "x", "market cap / book value"),
    ("dividend_yield", "Div. yield", "%", "dividend per share / price x 100"),
    ("return_on_equity", "ROE", "%", "net income / book value x 100"),
)

# Query parameter -> (figure, bound, label). A min keeps value >= threshold,
# a max value <= threshold; both inclusive.
FILTERS = {
    "pe_max": ("pe_ratio", "max", "P/E"),
    "pb_max": ("price_to_book", "max", "P/B"),
    "yield_min": ("dividend_yield", "min", "dividend yield"),
    "roe_min": ("return_on_equity", "min", "ROE"),
    "market_cap_min": ("market_cap", "min", "market cap"),
}

# Sort -> True if highest first.
DESCENDING = {
    ScreenerSort.dividend_yield: True,
    ScreenerSort.pe_ratio: False,
    ScreenerSort.price_to_book: False,
    ScreenerSort.return_on_equity: True,
    ScreenerSort.market_cap: True,
}


@dataclass(frozen=True)
class Entry:
    """One company to screen: its instrument master name and sector, and
    calculated_figures() for it."""
    symbol: str
    name: str
    sector: str
    figures: dict


def _figure(figures: dict, key: str) -> ScreenerFigure:
    f = figures[key]
    return ScreenerFigure(value=f["value"], formula=f["formula"], reason=f["reason"])


def _row(e: Entry) -> ScreenerRow:
    return ScreenerRow(
        symbol=e.symbol, name=e.name, sector=e.sector,
        **{key: _figure(e.figures, key) for key, *_ in COLUMNS},
    )


def _note(count: int, reasons: dict[str, int], label: str) -> Optional[str]:
    """'1 company was left out because it has negative earnings.'"""
    if not count:
        return None
    subject = "1 company was" if count == 1 else f"{count} companies were"
    has = "it has" if count == 1 else "they have"
    if len(reasons) == 1:
        return f"{subject} left out because {has} {next(iter(reasons))}."
    parts = ", ".join(f"{n} {reason}" for reason, n in reasons.items())
    return f"{subject} left out because {has} no {label}: {parts}."


def screen(
    entries: Iterable[Entry],
    sector: Optional[str] = None,
    sort: ScreenerSort = ScreenerSort.dividend_yield,
    **bounds: Optional[float],
) -> ScreenerResult:
    """Filter `entries` by `sector` (case-insensitive) and the FILTERS
    bounds given (None or left out: not set), then sort. Raises
    ValueError for an unknown sector or filter."""
    unknown = set(bounds) - set(FILTERS)
    if unknown:
        raise ValueError(f"unknown filter(s): {', '.join(sorted(unknown))}")
    entries = list(entries)
    sectors = sorted({e.sector for e in entries})

    if sector is not None:
        match = next((s for s in sectors if s.lower() == sector.strip().lower()), None)
        if match is None:
            raise ValueError(f"no company in sector {sector!r}; sectors are: {', '.join(sectors)}")
        sector = match
        entries = [e for e in entries if e.sector == sector]

    applied, kept = [], entries
    for param, (key, bound, label) in FILTERS.items():
        threshold = bounds.get(param)
        if threshold is None:
            continue
        missing = sorted((e for e in entries if e.figures[key]["value"] is None), key=lambda e: e.symbol)
        reasons: dict[str, int] = {}
        for e in missing:
            reason = e.figures[key]["reason"] or "no value"
            reasons[reason] = reasons.get(reason, 0) + 1
        reasons = dict(sorted(reasons.items(), key=lambda r: (-r[1], r[0])))
        applied.append(ScreenerFilter(
            param=param, figure=key, label=label, bound=bound, threshold=threshold,
            left_out=len(missing), reasons=reasons, symbols=[e.symbol for e in missing],
            note=_note(len(missing), reasons, label),
        ))

        def passes(e: Entry) -> bool:
            value = e.figures[key]["value"]
            if value is None:
                return False
            return value >= threshold if bound == "min" else value <= threshold
        kept = [e for e in kept if passes(e)]

    kept.sort(key=lambda e: e.symbol)
    if sort != ScreenerSort.symbol:
        key, descending = sort.value, DESCENDING[sort]

        def order(e: Entry):
            value = e.figures[key]["value"]
            return (value is None, 0 if value is None else (-value if descending else value))
        kept.sort(key=order)  # stable: ties stay by symbol

    return ScreenerResult(
        currency=CURRENCY,
        sector=sector,
        sort=sort,
        universe=len(entries),
        count=len(kept),
        filters=applied,
        columns=[ScreenerColumn(key=key, label=label, unit=unit, formula=formula)
                 for key, label, unit, formula in COLUMNS],
        sectors=sectors,
        rows=[_row(e) for e in kept],
    )
