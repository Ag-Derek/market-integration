"""
Bond math for Ghana fixed income: T-bills and fixed-coupon GoG notes and
bonds. The conventions are the ones confirmed in the #33 spike; see
docs/fixed-income-sources-and-conventions.md.

Units throughout: yields and coupons are % a year (12.22, not 0.1222),
prices are per 100 face, durations are in years.
"""

from app.bond_math.bills import (
    bill_dv01,
    bill_invoice,
    bill_modified_duration,
    bill_price,
    bill_yield,
    discount_rate_to_yield,
    yield_to_discount_rate,
)
from app.bond_math.bonds import (
    Bond,
    accrued_interest,
    bond_invoice,
    bond_yield,
    clean_price,
    convexity,
    days_accrued,
    dirty_price,
    dv01,
    macaulay_duration,
    modified_duration,
    next_coupon_date,
    previous_coupon_date,
    supports,
)
from app.bond_math.conventions import BILL_DAY_BASIS, SETTLEMENT_LAG, DayCount, settlement_date
from app.bond_math.invoice import Invoice

__all__ = [
    "BILL_DAY_BASIS", "SETTLEMENT_LAG", "Bond", "DayCount", "Invoice",
    "accrued_interest", "bill_dv01", "bill_invoice", "bill_modified_duration", "bill_price",
    "bill_yield", "bond_invoice", "bond_yield", "clean_price", "convexity", "days_accrued",
    "dirty_price", "discount_rate_to_yield", "dv01", "macaulay_duration", "modified_duration",
    "next_coupon_date", "previous_coupon_date", "settlement_date", "supports",
    "yield_to_discount_rate",
]
