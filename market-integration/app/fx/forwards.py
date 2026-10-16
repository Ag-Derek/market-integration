"""
Cross rates and synthetic forwards from the Bank of Ghana fixes.

**Cross rates.** BoG fixes every currency against the cedi only, so a
cross goes through it: units of B per unit of A = (GHS per A) / (GHS per
B). At the mid that is exact, and the matrix is consistent by
construction (A->B->C equals A->C; A->B is 1 / B->A).

**Synthetic forwards.** Ghana has no traded forward curve we can read,
so the forward is what covered interest parity says it must be: borrow
one currency, convert at spot, deposit the other, and the forward that
leaves no profit is

    F = S x (1 + r_GHS x d / 364) / (1 + r_FOR x d / basis)

with S in cedis per unit (the BoG mid), d the days to the value date,
r_GHS the cedi T-bill yield for that tenor (simple, ACT/364, as GFIM
quotes bills) and r_FOR the foreign overnight benchmark (simple, on the
benchmark's own day count; see overnight.py).

The cedi rate comes from the latest 91, 182 and 364-day auction yields,
interpolated linearly in days between them and held flat beyond them
(so 1W and 1M use the 91-day yield). The foreign rate is the overnight
rate held flat for the whole tenor: no term premium, no expected moves.
Both are simplifications; the result is an indicative mid, not a dealer
quote. See docs/fx-rates.md.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Sequence

from app.bond_math.conventions import add_months

GHS_BASIS = 364  # T-bills: simple interest, ACT/364
# Rates are fixed to 4 decimal places, so a pip is 0.0001.
PIP = 1e-4

# (label, months, days): the value date is the spot date moved by both.
TENORS: tuple[tuple[str, int, int], ...] = (
    ("1W", 0, 7), ("1M", 1, 0), ("2M", 2, 0), ("3M", 3, 0), ("6M", 6, 0), ("9M", 9, 0), ("1Y", 12, 0),
)


def cross_matrix(ghs_per_unit: Sequence[float]) -> list[list[float]]:
    """m[i][j]: units of currency j one unit of currency i buys, from
    each currency's cedis per unit (1 for the cedi itself)."""
    return [[a / b for b in ghs_per_unit] for a in ghs_per_unit]


def interpolate(curve: Sequence[tuple[int, float]], days: int) -> float:
    """The rate at `days` on a curve of (days, rate) points: linear
    between points, flat beyond the first and last."""
    points = sorted(curve)
    if not points:
        raise ValueError("empty curve")
    if days <= points[0][0]:
        return points[0][1]
    for (d0, r0), (d1, r1) in zip(points, points[1:]):
        if days <= d1:
            return r0 + (r1 - r0) * (days - d0) / (d1 - d0)
    return points[-1][1]


def synthetic_forward(spot: float, days: int, ghs_rate: float, foreign_rate: float, foreign_basis: int) -> float:
    """The covered-interest-parity forward, cedis per unit, `days` after
    spot. Rates are % a year."""
    return spot * (1 + ghs_rate / 100 * days / GHS_BASIS) / (1 + foreign_rate / 100 * days / foreign_basis)


@dataclass(frozen=True)
class ForwardPoint:
    tenor: str
    value_date: date
    days: int
    ghs_rate: float        # % a year, ACT/364
    foreign_rate: float    # % a year, on the benchmark's day count
    outright: float        # cedis per unit
    points: float          # (outright - spot) in pips
    premium_pa: float      # (outright / spot - 1), annualised on 365 days, %:
                           # the cedi depreciation the rates imply


def forward_curve(spot: float, spot_date: date, ghs_curve: Sequence[tuple[int, float]],
                  foreign_rate: float, foreign_basis: int) -> list[ForwardPoint]:
    out = []
    for label, months, extra_days in TENORS:
        value_date = add_months(spot_date, months) + timedelta(days=extra_days)
        days = (value_date - spot_date).days
        ghs_rate = interpolate(ghs_curve, days)
        outright = synthetic_forward(spot, days, ghs_rate, foreign_rate, foreign_basis)
        out.append(ForwardPoint(
            tenor=label, value_date=value_date, days=days, ghs_rate=ghs_rate, foreign_rate=foreign_rate,
            outright=outright, points=(outright - spot) / PIP,
            premium_pa=(outright / spot - 1) * 365 / days * 100,
        ))
    return out
