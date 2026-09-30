"""
Fixed-coupon bullet bonds: price <-> yield, accrued interest, duration,
convexity, DV01 and the trade invoice.

Conventions (docs/fixed-income-sources-and-conventions.md):

  * coupons are paid `frequency` times a year on the maturity date's day
    of the month, stepping back from maturity, unadjusted for holidays;
  * yields are compounded at the coupon frequency, with the fractional
    period to the next coupon discounted as a fraction of a period
    (ICMA). The final period is compounded too -- Excel's PRICE switches
    to simple interest when one coupon is left, so the two differ there;
  * prices are clean or dirty per 100 face; yields and coupons are % a
    year, as in the GFIM report.

The GFIM report values bonds at the trade date; an invoice for a real
trade uses the T+2 settlement date (conventions.settlement_date).
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from app.bond_math.conventions import (
    GOG_BOND_DAY_COUNT,
    GOG_BOND_FREQUENCY,
    DayCount,
    add_months,
    days_30_360,
)
from app.bond_math.invoice import Invoice
from app.models.instrument import Instrument

_YIELD_TOLERANCE = 1e-12  # % a year
_MAX_ITERATIONS = 200
_MAX_YIELD = 10_000.0     # % a year; far above any GFIM print (the sample's highest is ~59%)


@dataclass(frozen=True)
class Bond:
    maturity: date
    coupon: float                          # % a year
    frequency: int = GOG_BOND_FREQUENCY
    day_count: DayCount = GOG_BOND_DAY_COUNT
    redemption: float = 100.0

    def __post_init__(self):
        if self.frequency not in (1, 2, 4, 12):
            raise ValueError(f"frequency must be 1, 2, 4 or 12, got {self.frequency}")

    @classmethod
    def from_instrument(cls, inst: Instrument) -> "Bond":
        """A GoG note or bond from the instrument master. Refuses what the
        v1 conventions don't cover: bills (use the bill functions),
        corporates, the GFSF and USD DDE bonds, and anything without a
        coupon or maturity."""
        reason = _out_of_scope(inst)
        if reason:
            raise ValueError(f"{inst.symbol} ({inst.name}): {reason}")
        return cls(maturity=inst.maturity_date, coupon=inst.coupon_rate)


def supports(inst: Instrument) -> bool:
    """Whether Bond.from_instrument() accepts `inst`."""
    return _out_of_scope(inst) is None


def _out_of_scope(inst: Instrument) -> Optional[str]:
    if inst.asset_class != "bond":
        return "not a bond"
    if inst.segment not in ("new_gog", "ddep", "old_gog"):
        return "corporate bonds are display-only in v1 (no term sheets)"
    if inst.tenor and inst.tenor.upper().startswith(("GFSF", "USD-DDE")):
        return "GFSF and USD DDE bonds don't price as plain bullets; needs term sheets"
    if inst.maturity_date is None or inst.coupon_rate is None:
        return "no maturity date or coupon"
    return None


# ---------------------------------------------------------------- periods

@dataclass(frozen=True)
class _Period:
    previous_coupon: date
    next_coupon: date
    coupons_left: int         # including the next one
    days_accrued: int
    accrued_fraction: float   # of a coupon period, for accrued interest
    to_next: float            # periods from settlement to the next coupon


def _period(bond: Bond, settle: date) -> _Period:
    if settle >= bond.maturity:
        raise ValueError(f"settlement {settle} must be before maturity {bond.maturity}")
    step = 12 // bond.frequency
    k = 1
    while add_months(bond.maturity, -step * k) > settle:
        k += 1
    previous = add_months(bond.maturity, -step * k)
    next_ = add_months(bond.maturity, -step * (k - 1))

    if bond.day_count == "30/360":  # as Excel: DSC = E - A
        days_accrued = days_30_360(previous, settle)
        accrued_fraction = days_accrued / (360 / bond.frequency)
        to_next = 1 - accrued_fraction
    else:
        days_accrued = (settle - previous).days
        if bond.day_count == "ACT/ACT":
            period_days = (next_ - previous).days
        else:
            period_days = (365 if bond.day_count == "ACT/365" else 364) / bond.frequency
        accrued_fraction = days_accrued / period_days
        to_next = (next_ - settle).days / period_days
    return _Period(previous, next_, k, days_accrued, accrued_fraction, to_next)


def _cash_flows(bond: Bond, p: _Period) -> list[tuple[float, float]]:
    """(periods from settlement, amount per 100 face) for every remaining
    payment."""
    c = bond.coupon / bond.frequency
    flows = [(i + p.to_next, c) for i in range(p.coupons_left)]
    flows[-1] = (flows[-1][0], c + bond.redemption)
    return flows


def _check_yield(bond: Bond, yield_pct: float) -> float:
    per_period = yield_pct / 100 / bond.frequency
    if per_period <= -1:
        raise ValueError(f"yield {yield_pct}% is below -{100 * bond.frequency}%")
    return per_period


# ---------------------------------------------------------------- accrued

def previous_coupon_date(bond: Bond, settle: date) -> date:
    return _period(bond, settle).previous_coupon


def next_coupon_date(bond: Bond, settle: date) -> date:
    return _period(bond, settle).next_coupon


def days_accrued(bond: Bond, settle: date) -> int:
    return _period(bond, settle).days_accrued


def accrued_interest(bond: Bond, settle: date) -> float:
    """Per 100 face."""
    return bond.coupon / bond.frequency * _period(bond, settle).accrued_fraction


# ---------------------------------------------------------------- price <-> yield

def dirty_price(bond: Bond, settle: date, yield_pct: float) -> float:
    r = _check_yield(bond, yield_pct)
    return sum(cf / (1 + r) ** n for n, cf in _cash_flows(bond, _period(bond, settle)))


def clean_price(bond: Bond, settle: date, yield_pct: float) -> float:
    return dirty_price(bond, settle, yield_pct) - accrued_interest(bond, settle)


def bond_yield(bond: Bond, settle: date, clean: float) -> float:
    """The yield (% a year) at which the bond's clean price is `clean`.
    Newton's method, falling back to bisection inside a bracket that
    always holds the root, since price falls as yield rises."""
    p = _period(bond, settle)
    flows = _cash_flows(bond, p)
    target = clean + bond.coupon / bond.frequency * p.accrued_fraction
    if target <= 0:
        raise ValueError(f"clean price {clean} gives a non-positive dirty price")

    f = bond.frequency
    lo, hi = -100.0 * f + 1e-9, _MAX_YIELD
    if _dirty(flows, hi, f) > target:
        raise ValueError(f"clean price {clean} implies a yield above {_MAX_YIELD}%")
    y = bond.coupon if bond.coupon > 0 else 10.0
    for _ in range(_MAX_ITERATIONS):
        price, slope = _dirty(flows, y, f), _slope(flows, y, f)
        if price > target:
            lo = y
        else:
            hi = y
        if price == target:
            return y
        candidate = y - (price - target) / slope
        if not lo < candidate < hi:
            candidate = (lo + hi) / 2
        if abs(candidate - y) < _YIELD_TOLERANCE:
            return candidate
        y = candidate
    raise ArithmeticError(f"yield did not converge for clean price {clean}")


def _dirty(flows, yield_pct: float, f: int) -> float:
    r = yield_pct / 100 / f
    return sum(cf / (1 + r) ** n for n, cf in flows)


def _slope(flows, yield_pct: float, f: int) -> float:
    """d(dirty)/d(yield %)."""
    r = yield_pct / 100 / f
    return -sum(n * cf / (1 + r) ** (n + 1) for n, cf in flows) / (100 * f)


# ---------------------------------------------------------------- risk

def macaulay_duration(bond: Bond, settle: date, yield_pct: float) -> float:
    """Years (periods / frequency), weighted by present value."""
    r = _check_yield(bond, yield_pct)
    flows = _cash_flows(bond, _period(bond, settle))
    pv = [(n, cf / (1 + r) ** n) for n, cf in flows]
    return sum(n * v for n, v in pv) / sum(v for _, v in pv) / bond.frequency


def modified_duration(bond: Bond, settle: date, yield_pct: float) -> float:
    """-(dP/dy) / P, in years, for y as a decimal."""
    r = _check_yield(bond, yield_pct)
    return macaulay_duration(bond, settle, yield_pct) / (1 + r)


def convexity(bond: Bond, settle: date, yield_pct: float) -> float:
    """(d2P/dy2) / P, in years squared, for y as a decimal."""
    r = _check_yield(bond, yield_pct)
    f = bond.frequency
    flows = _cash_flows(bond, _period(bond, settle))
    price = sum(cf / (1 + r) ** n for n, cf in flows)
    return sum(cf * n * (n + 1) / (1 + r) ** (n + 2) for n, cf in flows) / (price * f * f)


def dv01(bond: Bond, settle: date, yield_pct: float) -> float:
    """Price change per 100 face for a 1bp fall in yield."""
    return modified_duration(bond, settle, yield_pct) * dirty_price(bond, settle, yield_pct) / 10_000


# ---------------------------------------------------------------- invoice

def bond_invoice(bond: Bond, settle: date, face: float, clean: float) -> Invoice:
    """What a buyer pays for `face` nominal at clean price `clean`."""
    p = _period(bond, settle)
    principal = face * clean / 100
    accrued = face * bond.coupon / bond.frequency * p.accrued_fraction / 100
    return Invoice(settlement_date=settle, face=face, clean_price=clean, days_accrued=p.days_accrued,
                   principal=principal, accrued=accrued, total=principal + accrued)
