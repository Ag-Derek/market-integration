"""
Ghana treasury bills: zero-coupon, quoted as a simple interest rate on
ACT/364 (not a discount rate):

    price = 100 / (1 + y * days / 364)

`days` runs from the valuation date to maturity. The GFIM report values
bills at the trade date; an invoice for a real trade uses the T+2
settlement date (conventions.settlement_date).

Rates are % a year and prices per 100 face, as in the GFIM report.
"""

from datetime import date

from app.bond_math.conventions import BILL_DAY_BASIS
from app.bond_math.invoice import Invoice


def _days(settle: date, maturity: date) -> int:
    days = (maturity - settle).days
    if days <= 0:
        raise ValueError(f"settlement {settle} must be before maturity {maturity}")
    return days


def bill_price(settle: date, maturity: date, yield_pct: float) -> float:
    return 100 / (1 + yield_pct / 100 * _days(settle, maturity) / BILL_DAY_BASIS)


def bill_yield(settle: date, maturity: date, price: float) -> float:
    if price <= 0:
        raise ValueError(f"price must be positive, got {price}")
    return (100 / price - 1) * BILL_DAY_BASIS / _days(settle, maturity) * 100


def discount_rate_to_yield(discount_pct: float, days: int) -> float:
    """BoG publishes both rates for each auction; GFIM quotes the yield."""
    return discount_pct / (1 - discount_pct / 100 * days / BILL_DAY_BASIS)


def yield_to_discount_rate(yield_pct: float, days: int) -> float:
    return yield_pct / (1 + yield_pct / 100 * days / BILL_DAY_BASIS)


def bill_modified_duration(settle: date, maturity: date, yield_pct: float) -> float:
    """-(dP/dy) / P in years, with a year of 364 days to match the
    pricing formula."""
    t = _days(settle, maturity) / BILL_DAY_BASIS
    return t / (1 + yield_pct / 100 * t)


def bill_dv01(settle: date, maturity: date, yield_pct: float) -> float:
    """Price change per 100 face for a 1bp fall in yield."""
    return bill_modified_duration(settle, maturity, yield_pct) * bill_price(settle, maturity, yield_pct) / 10_000


def bill_invoice(settle: date, maturity: date, face: float, price: float) -> Invoice:
    """What a buyer pays for `face` of a bill at `price`. Bills don't
    accrue, so the total is the principal."""
    _days(settle, maturity)
    principal = face * price / 100
    return Invoice(settlement_date=settle, face=face, clean_price=price, days_accrued=0,
                   principal=principal, accrued=0.0, total=principal)
