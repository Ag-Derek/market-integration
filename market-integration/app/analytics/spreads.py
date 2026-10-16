"""
Spread analysis behind /spreads: where each bill, bond and corporate
sits against a smooth Government of Ghana curve. Pure functions over a
GovernmentYieldCurve and corporate closes, so they can be tested on
hand-made data; app/main.py takes both from the fixed-income market.

**The fitted curve.** A Nelson-Siegel-Svensson curve is fitted by least
squares to the closing yields of the bills, New GoG and 2023 DDEP bonds.
Svensson rather than plain Nelson-Siegel (the mock's form,
app/connectors/yield_curve.py) because the GHS bill curve bends hard
inside a year -- the 28-Sep-2026 sample runs 9.4% at a week, 4.7% at 91
days, 9.8% at 364 -- and one hump with a fixed decay can't follow that,
so every bill would read as rich or cheap. The two decays are chosen by
grid search; for each pair the four factors are a linear least-squares
fit. Old GoG bonds trade rarely and their closes are often stale, so
they are measured against the curve but don't shape it. Raw points
zig-zag (each bond trades at its own spread), so a spread to the fitted
curve is a steadier yardstick than one to the nearest bond.

**Government spreads** are each point's residual: its yield minus the
fitted yield at its tenor. Positive means it yields more than the curve
says it should (cheap), negative less (rich).

**Corporate spreads.** GFIM quotes corporates by price only and we have
no term sheets (docs/fixed-income-sources-and-conventions.md, decision
6), so each corporate's yield is worked out from its closing clean price
*assuming* GoG bond conventions: a semi-annual fixed-coupon bullet, ACT/ACT,
valued at the session date as the report values bonds. That makes the
spread indicative: a bond that actually pays quarterly or amortises
would come out a little different. Three corporates have no coupon in
their description; they get no yield, and say why.

Not covered: Z-spread and OAS (they need a zero curve bootstrapped from
the bills and bonds, and term sheets for anything callable).
"""

import math
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

from app.bond_math.bonds import Bond, bond_yield
from app.models.fixed_income import (
    CorporateSpread,
    FittedCurve,
    GovernmentSpread,
    GovernmentYieldCurve,
    IssuerSpread,
    SpreadAnalysis,
)
from app.models.instrument import Instrument

FIT_SEGMENTS = ("treasury_bill", "new_gog", "ddep")
# Four factors need more points than that to say anything.
MIN_FIT_POINTS = 6
CURVE_SAMPLES = 80
_DAYS_PER_YEAR = 365.25
# Candidate decays (years), log-spaced from ~2 weeks to 10 years; the
# fit tries every pair with the first shorter than the second.
DECAY_GRID = tuple(round(0.04 * (10 / 0.04) ** (i / 23), 4) for i in range(24))
# Small ridge on everything but the level, so a pair of near-equal
# decays (nearly collinear loadings) can't send the factors to +/- huge.
_RIDGE = 1e-4

ASSUMPTIONS = [
    "Fitted curve: Nelson-Siegel-Svensson, least squares on the closing yields of Treasury bills, "
    "New GoG and 2023 DDEP bonds, with both decays chosen by grid search. Old GoG bonds are measured "
    "against it but not fitted to.",
    "Corporate yields are worked out from the closing clean price as semi-annual ACT/ACT fixed-coupon "
    "bullets (GoG conventions). GFIM publishes no corporate yields and we have no term sheets, so "
    "corporate spreads are indicative.",
    "Spreads are in basis points of yield over the fitted curve at the same tenor: positive is cheap "
    "(above the curve), negative rich.",
]


def spread_analysis(
    curve: GovernmentYieldCurve,
    corporates: Sequence[tuple[Instrument, float]],
) -> SpreadAnalysis:
    """The fitted curve, every government point's residual to it and
    every corporate's spread over it, for one session. `corporates` is
    (instrument, closing clean price) for the bonds outstanding then."""
    fit_on = [(p.tenor_years, p.yield_) for p in curve.points if p.segment in FIT_SEGMENTS]
    base = dict(date=curve.date, as_of=curve.as_of, currency=curve.currency,
                exchange_label=curve.exchange_label, assumptions=ASSUMPTIONS)
    if len(fit_on) < MIN_FIT_POINTS:
        return SpreadAnalysis(**base, curve=None, government=[], corporate=[], issuers=[])

    fit = fit_svensson(fit_on)
    fitted = fit.yield_at

    government = [
        GovernmentSpread(
            symbol=p.instruments[0].symbol,
            description=", ".join(i.description for i in p.instruments),
            segment=p.segment, maturity_date=p.maturity_date, tenor_years=p.tenor_years,
            yield_=p.yield_, fitted_yield=_r(fitted(p.tenor_years)),
            spread_bp=_bp(p.yield_ - fitted(p.tenor_years)),
            in_fit=p.segment in FIT_SEGMENTS,
        )
        for p in curve.points
    ]
    by_segment: dict[str, list[float]] = {}
    for g in government:
        if g.in_fit:
            by_segment.setdefault(g.segment, []).append(g.spread_bp)
    b = fit.factors
    fitted_curve = FittedCurve(
        decay_years=fit.decays,
        level=_r(b[0]), slope=_r(b[1]), curvature=_r(b[2]), curvature_2=_r(b[3]),
        fit_points=len(fit_on),
        rmse_bp=_bp(fit.rmse),
        segment_rmse_bp={seg: round(math.sqrt(sum(s * s for s in v) / len(v)), 1) for seg, v in by_segment.items()},
        samples=_samples(fitted, [t for t, _ in fit_on]),
    )

    corporate = sorted(
        (_corporate(inst, price, curve.date, fitted) for inst, price in corporates
         if inst.maturity_date is not None and inst.maturity_date > curve.date),
        key=lambda c: (c.tenor_years, c.symbol),
    )
    return SpreadAnalysis(**base, curve=fitted_curve, government=government,
                          corporate=corporate, issuers=issuer_spreads(corporate))


@dataclass(frozen=True)
class SvenssonFit:
    factors: tuple[float, float, float, float]   # level, slope, curvature, second curvature (% a year)
    decays: tuple[float, float]                  # years, shorter first
    rmse: float                                  # % points, over the fitted points

    def yield_at(self, years: float) -> float:
        return sum(b * x for b, x in zip(self.factors, _loadings(years, self.decays)))


def fit_svensson(points: Sequence[tuple[float, float]]) -> SvenssonFit:
    """The Svensson curve with the least squared error through (years,
    yield %) points, over every pair of decays in DECAY_GRID. Needs at
    least MIN_FIT_POINTS points."""
    if len(points) < MIN_FIT_POINTS:
        raise ValueError(f"need at least {MIN_FIT_POINTS} points to fit, got {len(points)}")
    best = None
    for i, short in enumerate(DECAY_GRID):
        for long in DECAY_GRID[i + 1:]:
            factors = _least_squares(points, (short, long))
            if factors is None:
                continue
            sse = sum((y - sum(b * x for b, x in zip(factors, _loadings(t, (short, long))))) ** 2
                      for t, y in points)
            if best is None or sse < best[0]:
                best = (sse, factors, (short, long))
    if best is None:
        raise ArithmeticError("no decay pair gave a solvable fit")
    sse, factors, decays = best
    return SvenssonFit(factors, decays, math.sqrt(sse / len(points)))


def _loadings(years: float, decays: tuple[float, float]) -> tuple[float, float, float, float]:
    x1, x2 = max(years, 1e-6) / decays[0], max(years, 1e-6) / decays[1]
    slope = (1 - math.exp(-x1)) / x1
    return 1.0, slope, slope - math.exp(-x1), (1 - math.exp(-x2)) / x2 - math.exp(-x2)


def _least_squares(points, decays) -> Optional[tuple[float, ...]]:
    """Normal equations (X'X + ridge) b = X'y by Gaussian elimination with
    partial pivoting; None if singular."""
    n = 4
    a = [[0.0] * n for _ in range(n)]
    rhs = [0.0] * n
    for years, y in points:
        row = _loadings(years, decays)
        for i in range(n):
            rhs[i] += row[i] * y
            for j in range(n):
                a[i][j] += row[i] * row[j]
    for i in range(1, n):
        a[i][i] += _RIDGE * len(points)
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        if abs(a[pivot][col]) < 1e-12:
            return None
        a[col], a[pivot] = a[pivot], a[col]
        rhs[col], rhs[pivot] = rhs[pivot], rhs[col]
        for r in range(col + 1, n):
            f = a[r][col] / a[col][col]
            for c in range(col, n):
                a[r][c] -= f * a[col][c]
            rhs[r] -= f * rhs[col]
    b = [0.0] * n
    for r in reversed(range(n)):
        b[r] = (rhs[r] - sum(a[r][c] * b[c] for c in range(r + 1, n))) / a[r][r]
    return tuple(b)


def corporate_yield(inst: Instrument, price: float, on: date) -> tuple[Optional[float], Optional[str]]:
    """(yield % a year, None), or (None, why not), for a corporate's clean
    `price` on `on`, as a semi-annual ACT/ACT bullet."""
    if inst.coupon_rate is None:
        return None, "no coupon in the description; needs the term sheet"
    try:
        return bond_yield(Bond(inst.maturity_date, inst.coupon_rate), on, price), None
    except (ValueError, ArithmeticError) as e:
        return None, str(e)


def issuer_spreads(corporates: Sequence[CorporateSpread]) -> list[IssuerSpread]:
    """Per issuer, over its bonds that have a spread; widest first."""
    by_issuer: dict[str, list[float]] = {}
    for c in corporates:
        if c.spread_bp is not None:
            by_issuer.setdefault(c.issuer, []).append(c.spread_bp)
    out = [
        IssuerSpread(issuer=issuer, bonds=len(s), mean_spread_bp=round(sum(s) / len(s), 1),
                     min_spread_bp=min(s), max_spread_bp=max(s))
        for issuer, s in by_issuer.items()
    ]
    return sorted(out, key=lambda i: (-i.mean_spread_bp, i.issuer))


def _corporate(inst: Instrument, price: float, on: date, fitted) -> CorporateSpread:
    years = round((inst.maturity_date - on).days / _DAYS_PER_YEAR, 4)
    y, reason = corporate_yield(inst, price, on)
    curve_yield = fitted(years)
    return CorporateSpread(
        symbol=inst.symbol, description=inst.name, issuer=inst.issuer or inst.name, sector=inst.sector,
        maturity_date=inst.maturity_date, tenor_years=years, coupon=inst.coupon_rate,
        clean_price=_r(price), yield_=_r(y), fitted_yield=_r(curve_yield),
        spread_bp=None if y is None else _bp(y - curve_yield), reason=reason,
    )


def _samples(fitted, tenors: list[float]) -> list[tuple[float, float]]:
    """The fitted curve at CURVE_SAMPLES tenors, evenly spaced in log
    tenor across the fitted points (the page draws tenor on a log scale).
    Not beyond them: Nelson-Siegel extrapolates flat, but the data says
    nothing out there."""
    lo, hi = math.log(max(min(tenors), 1 / 365)), math.log(max(tenors))
    if hi <= lo:
        return [(round(math.exp(lo), 4), _r(fitted(math.exp(lo))))]
    step = (hi - lo) / (CURVE_SAMPLES - 1)
    return [(round(math.exp(lo + i * step), 4), _r(fitted(math.exp(lo + i * step))))
            for i in range(CURVE_SAMPLES)]


def _bp(pct: float) -> float:
    return round(pct * 100, 1)


def _r(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 4)
