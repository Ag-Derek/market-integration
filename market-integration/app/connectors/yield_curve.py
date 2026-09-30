"""
A drifting yield curve for the fixed-income mock.

The curve is Nelson-Siegel: level, slope and curvature factors on fixed
loadings of maturity, fitted once to the calibration closes. Each factor
then follows a mean-reverting (Ornstein-Uhlenbeck) random walk around
its fitted value, so the whole curve shifts, steepens and bends over
time while staying smooth and plausible. Individual securities sit at a
spread to it (see fixed_income_mock.py), which drifts the same way.

Time is in days, yields in % a year, maturities in years.
"""

import math
import random
from dataclasses import dataclass
from typing import Optional, Sequence

# Where the slope's loading has decayed by 1/e. 1.5 years puts the hump
# between the bill tenors and the short bonds, where the GFIM curve bends.
NS_LAMBDA_YEARS = 1.5
# Small ridge on slope and curvature, so a fit to a handful of points
# (the four USD bonds) stays tame.
_RIDGE = 1e-3


def _loadings(years: float) -> tuple[float, float, float]:
    x = max(years, 1e-6) / NS_LAMBDA_YEARS
    slope = (1 - math.exp(-x)) / x
    return 1.0, slope, slope - math.exp(-x)


@dataclass
class OrnsteinUhlenbeck:
    """A mean-reverting random walk, stepped exactly for any time step."""
    value: float
    mean: float
    daily_vol: float        # % points per sqrt(day)
    half_life_days: float

    def step(self, days: float, rng: random.Random = random) -> float:
        if days <= 0:
            return self.value
        decay = math.exp(-math.log(2) / self.half_life_days * days)
        theta = math.log(2) / self.half_life_days
        sd = self.daily_vol * math.sqrt((1 - decay ** 2) / (2 * theta))
        self.value = self.mean + (self.value - self.mean) * decay + rng.gauss(0, sd)
        return self.value


@dataclass
class YieldCurve:
    level: OrnsteinUhlenbeck
    slope: OrnsteinUhlenbeck
    curvature: OrnsteinUhlenbeck

    def yield_at(self, years: float) -> float:
        a, b, c = _loadings(years)
        return a * self.level.value + b * self.slope.value + c * self.curvature.value

    def factors(self) -> tuple[float, float, float]:
        return self.level.value, self.slope.value, self.curvature.value

    def set_factors(self, factors: tuple[float, float, float]) -> None:
        self.level.value, self.slope.value, self.curvature.value = factors

    def step(self, days: float, rng: random.Random = random) -> None:
        for factor in (self.level, self.slope, self.curvature):
            factor.step(days, rng)


def yield_from_factors(factors: tuple[float, float, float], years: float) -> float:
    a, b, c = _loadings(years)
    return a * factors[0] + b * factors[1] + c * factors[2]


def fit_curve(
    points: Sequence[tuple[float, float]],
    level_vol: float,
    slope_vol: float,
    curvature_vol: float,
    half_life_days: float,
    flat: bool = False,
) -> Optional[YieldCurve]:
    """Least-squares Nelson-Siegel fit to (years, yield %) points, or a
    flat curve at their mean. None without points."""
    if not points:
        return None
    if flat:
        factors = (sum(y for _, y in points) / len(points), 0.0, 0.0)
    else:
        factors = _least_squares(points)

    def ou(value: float, vol: float) -> OrnsteinUhlenbeck:
        return OrnsteinUhlenbeck(value, value, vol, half_life_days)

    return YieldCurve(
        ou(factors[0], level_vol),
        ou(factors[1], 0.0 if flat else slope_vol),
        ou(factors[2], 0.0 if flat else curvature_vol),
    )


def _least_squares(points: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    # Normal equations (X'X + ridge) b = X'y, solved by Gaussian elimination.
    a = [[0.0] * 3 for _ in range(3)]
    rhs = [0.0] * 3
    for years, y in points:
        row = _loadings(years)
        for i in range(3):
            rhs[i] += row[i] * y
            for j in range(3):
                a[i][j] += row[i] * row[j]
    a[1][1] += _RIDGE * len(points)
    a[2][2] += _RIDGE * len(points)
    for col in range(3):
        pivot = max(range(col, 3), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        rhs[col], rhs[pivot] = rhs[pivot], rhs[col]
        for r in range(col + 1, 3):
            f = a[r][col] / a[col][col]
            for c in range(col, 3):
                a[r][c] -= f * a[col][c]
            rhs[r] -= f * rhs[col]
    b = [0.0] * 3
    for r in reversed(range(3)):
        b[r] = (rhs[r] - sum(a[r][c] * b[c] for c in range(r + 1, 3))) / a[r][r]
    return b[0], b[1], b[2]
