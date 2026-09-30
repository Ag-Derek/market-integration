"""
Bond math against independent sources, each to a stated tolerance:

  * Excel's published worked examples (Microsoft support pages for
    PRICE, YIELD, DURATION and MDURATION), to the precision published;
  * QuantLib 1.43 (FixedRateBond, ActualActual ISMA or Thirty360 USA,
    Compounded Semiannual), generated once and pasted in -- QuantLib
    isn't a dependency. To 1e-8;
  * the GFIM daily report of 28-Sep-2026 and the BoG auction rates
    for the same day, to the precision the conventions reproduce them
    (docs/fixed-income-sources-and-conventions.md).
"""

import itertools
from datetime import date, time, timezone

import pytest

from app.bond_math import (
    Bond,
    accrued_interest,
    bill_dv01,
    bill_invoice,
    bill_modified_duration,
    bill_price,
    bill_yield,
    bond_invoice,
    bond_yield,
    clean_price,
    convexity,
    days_accrued,
    dirty_price,
    discount_rate_to_yield,
    dv01,
    macaulay_duration,
    modified_duration,
    next_coupon_date,
    previous_coupon_date,
    settlement_date,
    supports,
    yield_to_discount_rate,
)
from app.instruments import INSTRUMENTS
from app.session.calendar import MarketCalendar

REPORT_DATE = date(2026, 9, 28)
GC3 = Bond(date(2029, 2, 13), 8.65)       # 2023-GC-3
NEW_7Y = Bond(date(2033, 3, 29), 12.50)   # GOG-BD-29/03/33


# ---------------------------------------------------------------- Excel

def test_excel_price_example():
    # PRICE(15-Feb-2008, 15-Nov-2017, 5.75%, 6.50%, 100, 2, 0) = 94.63436162
    bond = Bond(date(2017, 11, 15), 5.75, day_count="30/360")
    assert clean_price(bond, date(2008, 2, 15), 6.50) == pytest.approx(94.63436162, abs=1e-8)


def test_excel_yield_example():
    # YIELD(15-Feb-2008, 15-Nov-2016, 5.75%, 95.04287, 100, 2, 0) = 6.5%
    bond = Bond(date(2016, 11, 15), 5.75, day_count="30/360")
    assert bond_yield(bond, date(2008, 2, 15), 95.04287) == pytest.approx(6.50, abs=5e-5)


def test_excel_duration_example():
    # DURATION(1-Jul-2018, 1-Jan-2048, 8%, 9%, 2, 1) = 10.9191453
    bond = Bond(date(2048, 1, 1), 8.0)
    assert macaulay_duration(bond, date(2018, 7, 1), 9.0) == pytest.approx(10.9191453, abs=5e-8)


def test_excel_mduration_example():
    # MDURATION(1-Jan-2008, 1-Jan-2016, 8%, 9%, 2, 1) = 5.736 (published to 3 dp)
    bond = Bond(date(2016, 1, 1), 8.0)
    assert modified_duration(bond, date(2008, 1, 1), 9.0) == pytest.approx(5.736, abs=5e-4)


# ---------------------------------------------------------------- QuantLib

QUANTLIB = [
    # bond, settlement, yield: clean, accrued, Macaulay, modified, convexity
    (GC3, date(2026, 9, 30), 13.63,
     90.1682271075, 1.1282608696, 2.1579056192, 2.0202271396, 5.2719784936),
    (NEW_7Y, date(2026, 9, 30), 12.17,
     101.4520521674, 0.0345303867, 4.6470308519, 4.3804787217, 25.4554681137),
    # one coupon left: compounded, not Excel's simple interest
    (Bond(date(2026, 11, 19), 6.00), date(2026, 9, 28), 6.6241,
     99.9037068045, 2.1521739130, 0.1413043478, 0.1367743142, 0.0849019667),
    (Bond(date(2017, 11, 15), 5.75, day_count="30/360"), date(2008, 2, 15), 6.50,
     94.6343616213, 1.4375, 7.4164846964, 7.1830360255, 64.8977445731),
]


@pytest.mark.parametrize("bond, settle, y, clean, accrued, mac, mod, cvx", QUANTLIB)
def test_matches_quantlib(bond, settle, y, clean, accrued, mac, mod, cvx):
    assert clean_price(bond, settle, y) == pytest.approx(clean, abs=1e-8)
    assert accrued_interest(bond, settle) == pytest.approx(accrued, abs=1e-8)
    assert macaulay_duration(bond, settle, y) == pytest.approx(mac, abs=1e-8)
    assert modified_duration(bond, settle, y) == pytest.approx(mod, abs=1e-8)
    assert convexity(bond, settle, y) == pytest.approx(cvx, abs=1e-7)
    assert bond_yield(bond, settle, clean) == pytest.approx(y, abs=1e-8)


# ---------------------------------------------------------------- GFIM and BoG

@pytest.mark.parametrize("maturity, closing_yield, closing_price", [
    # 91-, 182- and 364-day rows of the 28-Sep-2026 report, full precision
    (date(2026, 10, 5), 9.407674137931036, 99.81940990749034),
    (date(2027, 3, 29), 6.31005, 96.94147231315198),
    (date(2027, 6, 21), 8.565070279720281, 94.10959586571806),
])
def test_bills_reproduce_the_gfim_report_exactly(maturity, closing_yield, closing_price):
    assert bill_price(REPORT_DATE, maturity, closing_yield) == pytest.approx(closing_price, abs=1e-9)
    assert bill_yield(REPORT_DATE, maturity, closing_price) == pytest.approx(closing_yield, abs=1e-9)


@pytest.mark.parametrize("days, discount_rate, interest_rate", [
    # BoG tender 2026, issued 28-Sep-2026, published to 4 dp
    (91, 4.6244, 4.6785),
    (182, 6.1734, 6.3700),
    (364, 8.9534, 9.8339),
])
def test_bog_discount_and_interest_rates_convert_on_364_days(days, discount_rate, interest_rate):
    assert discount_rate_to_yield(discount_rate, days) == pytest.approx(interest_rate, abs=5e-5)
    assert yield_to_discount_rate(interest_rate, days) == pytest.approx(discount_rate, abs=5e-5)


GFIM_BONDS = [
    # New GoG and 2023 DDEP rows of the 28-Sep-2026 report:
    # maturity, coupon, closing yield, end-of-day closing price
    (date(2030, 9, 2), 12.00, 12.22, 99.30769231),
    (date(2033, 3, 29), 12.50, 12.17, 101.447848186795),
    (date(2027, 8, 17), 10.00, 11.00, 99.1526928912339),
    (date(2028, 8, 15), 10.00, 12.25, 96.2692999560439),
    (date(2027, 8, 17), 15.00, 15.20, 99.8086001327334),
    (date(2028, 8, 15), 15.00, 15.22, 99.62912088),
    (date(2027, 2, 16), 8.35, 11.10, 98.96373822),
    (date(2028, 2, 15), 8.50, 12.00, 95.62453793),
    (date(2029, 2, 13), 8.65, 13.63, 90.10205205),
    (date(2030, 2, 12), 8.80, 13.45, 87.65098723),
    (date(2031, 2, 11), 8.95, 14.22, 83.19669269),
    (date(2032, 2, 10), 9.10, 14.25, 81.05184935),
    (date(2033, 2, 8), 9.25, 14.22, 79.5035888305494),
    (date(2034, 2, 7), 9.40, 14.90, 75.8088156373626),
    (date(2035, 2, 6), 9.55, 14.65, 75.7933508791208),
    (date(2036, 2, 5), 9.70, 15.50, 71.7509210737578),
    (date(2037, 2, 3), 9.85, 15.50, 71.2349779859709),
    (date(2038, 2, 2), 10.00, 15.16, 72.36821325),
]


@pytest.mark.parametrize("maturity, coupon, closing_yield, closing_price", GFIM_BONDS)
def test_gog_bonds_are_within_5bp_of_the_gfim_report(maturity, coupon, closing_yield, closing_price):
    # GFIM hasn't published its bond convention; the spike found the
    # report's yields and prices agree with ours to within ~3bp.
    implied = bond_yield(Bond(maturity, coupon), REPORT_DATE, closing_price)
    assert implied == pytest.approx(closing_yield, abs=0.05)


# ---------------------------------------------------------------- round trips

ROUND_TRIP_BONDS = [
    GC3,
    NEW_7Y,
    Bond(date(2039, 8, 1), 20.20),                          # long, high coupon
    Bond(date(2026, 11, 19), 6.00),                         # one coupon left
    Bond(date(2031, 8, 31), 9.00),                          # month-end coupons
    Bond(date(2030, 6, 15), 7.00, frequency=1),
    Bond(date(2030, 6, 15), 7.00, frequency=4),
    Bond(date(2030, 6, 15), 0.0),                           # zero coupon
    Bond(date(2030, 6, 15), 7.00, day_count="ACT/365"),
    Bond(date(2030, 6, 15), 7.00, day_count="ACT/364"),
    Bond(date(2030, 6, 15), 7.00, day_count="30/360"),
]
ROUND_TRIP_SETTLES = [date(2026, 9, 28), date(2026, 2, 13), date(2026, 8, 13)]  # incl. coupon dates
ROUND_TRIP_YIELDS = [-0.5, 0.0, 0.01, 6.5, 13.63, 58.59, 150.0]


@pytest.mark.parametrize("bond, settle, y", [
    (b, s, y) for b, s, y in itertools.product(ROUND_TRIP_BONDS, ROUND_TRIP_SETTLES, ROUND_TRIP_YIELDS)
    if s < b.maturity
])
def test_bond_price_yield_price_round_trips(bond, settle, y):
    price = clean_price(bond, settle, y)
    assert clean_price(bond, settle, bond_yield(bond, settle, price)) == pytest.approx(price, abs=1e-9)


@pytest.mark.parametrize("maturity", [date(2026, 9, 29), date(2026, 12, 28), date(2027, 9, 27)])
@pytest.mark.parametrize("y", [0.0, 4.7043, 9.767, 45.0])
def test_bill_price_yield_price_round_trips(maturity, y):
    price = bill_price(REPORT_DATE, maturity, y)
    assert bill_price(REPORT_DATE, maturity, bill_yield(REPORT_DATE, maturity, price)) == pytest.approx(price, abs=1e-9)


# ---------------------------------------------------------------- accrued and coupons

def test_coupon_dates_and_accrued_for_2023_gc_3_at_t_plus_2():
    settle = date(2026, 9, 30)
    assert previous_coupon_date(GC3, settle) == date(2026, 8, 13)
    assert next_coupon_date(GC3, settle) == date(2027, 2, 13)
    assert days_accrued(GC3, settle) == 48
    # 4.325 coupon x 48 / 184 days in the period
    assert accrued_interest(GC3, settle) == pytest.approx(4.325 * 48 / 184, abs=1e-12)


def test_no_accrued_interest_on_a_coupon_date():
    assert accrued_interest(GC3, date(2026, 8, 13)) == 0
    assert clean_price(GC3, date(2026, 8, 13), 13.63) == dirty_price(GC3, date(2026, 8, 13), 13.63)


def test_month_end_maturity_steps_back_to_shorter_months():
    bond = Bond(date(2031, 8, 31), 9.00)
    assert previous_coupon_date(bond, date(2026, 3, 15)) == date(2026, 2, 28)
    assert next_coupon_date(bond, date(2026, 3, 15)) == date(2026, 8, 31)


def test_clean_price_is_dirty_less_accrued():
    settle = date(2026, 9, 30)
    assert dirty_price(GC3, settle, 13.63) - clean_price(GC3, settle, 13.63) == pytest.approx(
        accrued_interest(GC3, settle), abs=1e-12)


# ---------------------------------------------------------------- risk

@pytest.mark.parametrize("bond", [GC3, NEW_7Y, Bond(date(2026, 11, 19), 6.00)])
def test_dv01_and_convexity_match_finite_differences(bond):
    settle, y, h = date(2026, 9, 30), 13.63, 0.01  # h = 1bp in % terms
    up, mid, down = (dirty_price(bond, settle, y + d) for d in (h, 0, -h))
    assert dv01(bond, settle, y) == pytest.approx((down - up) / 2, rel=1e-6)
    # convexity is per unit of decimal yield squared
    assert convexity(bond, settle, y) == pytest.approx((up - 2 * mid + down) / (h / 100) ** 2 / mid, rel=1e-4)


def test_bill_risk_matches_finite_differences():
    maturity, y, h = date(2027, 6, 21), 8.565, 0.01
    up, mid, down = (bill_price(REPORT_DATE, maturity, y + d) for d in (h, 0, -h))
    assert bill_dv01(REPORT_DATE, maturity, y) == pytest.approx((down - up) / 2, rel=1e-6)
    assert bill_modified_duration(REPORT_DATE, maturity, y) == pytest.approx(
        (down - up) / (2 * h / 100) / mid, rel=1e-6)


# ---------------------------------------------------------------- invoice and settlement

def test_bond_invoice_for_a_face_amount():
    inv = bond_invoice(GC3, date(2026, 9, 30), face=1_000_000, clean=90.1021)
    assert inv.days_accrued == 48
    assert inv.principal == pytest.approx(901_021.00, abs=1e-6)
    assert inv.accrued == pytest.approx(1_000_000 * 0.0865 / 2 * 48 / 184, abs=1e-6)  # 11,282.61
    assert inv.total == pytest.approx(inv.principal + inv.accrued, abs=1e-9)


def test_bill_invoice_is_principal_only():
    inv = bill_invoice(date(2026, 9, 30), date(2027, 6, 21), face=5_000_000, price=94.1096)
    assert (inv.principal, inv.accrued, inv.total) == pytest.approx((4_705_480.0, 0.0, 4_705_480.0))


def _calendar(*holidays: date) -> MarketCalendar:
    return MarketCalendar(tz=timezone.utc, timezone_name="UTC", trading_days={0, 1, 2, 3, 4},
                          pre_open=time(9), open=time(10), close=time(15),
                          holidays={d: "Holiday" for d in holidays})


def test_settlement_is_t_plus_2_business_days():
    cal = _calendar(date(2026, 10, 1))
    assert settlement_date(date(2026, 9, 28), cal) == date(2026, 9, 30)            # Mon -> Wed
    assert settlement_date(date(2026, 9, 30), cal) == date(2026, 10, 5)            # skips the holiday and weekend
    assert settlement_date(date(2026, 9, 28), cal, lag=0) == date(2026, 9, 28)     # bilateral T+0


# ---------------------------------------------------------------- scope and errors

def _by_tenor(tenor: str):
    return next(i for i in INSTRUMENTS.values() if i.tenor == tenor)


def test_from_instrument_accepts_gog_bonds_and_refuses_the_rest():
    gc3 = _by_tenor("2023-GC-3")
    assert Bond.from_instrument(gc3) == GC3
    assert supports(gc3)
    for tenor in ("GFSF-2-6YR", "USD-DDE-FEA-28", "91-DAY BILL"):
        assert not supports(_by_tenor(tenor))
        with pytest.raises(ValueError):
            Bond.from_instrument(_by_tenor(tenor))
    corporate = next(i for i in INSTRUMENTS.values() if i.segment == "corporate")
    assert not supports(corporate)


def test_settlement_on_or_after_maturity_is_refused():
    with pytest.raises(ValueError):
        clean_price(GC3, GC3.maturity, 10.0)
    with pytest.raises(ValueError):
        bill_price(date(2026, 10, 5), date(2026, 10, 5), 9.0)


def test_unsupported_frequency_is_refused():
    with pytest.raises(ValueError):
        Bond(date(2030, 1, 1), 5.0, frequency=3)
