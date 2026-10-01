"""The bond detail page's calculator (#39): price <-> yield, risk and
invoice for a settlement date and face amount, and its API."""

from datetime import date, datetime, timezone

import pytest

from app.bond_math import Bond, bill_price, clean_price, dv01
from app.bond_math.calculator import Security, calculate, quoted_yield, security_for
from app.instruments import INSTRUMENTS
from app.models.fixed_income import FixedIncomeTick

GC3 = "GHGGOG069931"         # 2023-GC-3, 8.65% maturing 13-Feb-2029
LETSHEGO = "GHCLGH075744"    # a corporate, quoted by price only
SETTLE = date(2026, 9, 30)
GC3_BOND = Bond(date(2029, 2, 13), 8.65)


def _tick(symbol: str, **overrides) -> FixedIncomeTick:
    base = dict(symbol=symbol, name="TEST", segment="new_gog", currency="GHS",
                maturity_date=date(2029, 2, 13), timestamp=datetime.now(timezone.utc))
    base.update(overrides)
    return FixedIncomeTick(**base)


# ---------------------------------------------------------------- calculate

def test_yield_and_price_solve_each_other():
    sec = Security(GC3_BOND.maturity, GC3_BOND)
    by_yield = calculate(sec, SETTLE, 1_000_000, yield_pct=13.63)
    assert by_yield.solved_for == "price"
    assert by_yield.clean_price == pytest.approx(90.1682271075, abs=1e-8)   # QuantLib, as test_bond_math
    by_price = calculate(sec, SETTLE, 1_000_000, price=by_yield.clean_price)
    assert by_price.solved_for == "yield"
    assert by_price.yield_pct == pytest.approx(13.63, abs=1e-8)


def test_bond_risk_and_invoice():
    calc = calculate(Security(GC3_BOND.maturity, GC3_BOND), SETTLE, 1_000_000, yield_pct=13.63)
    assert calc.modified_duration == pytest.approx(2.0202271396, abs=1e-8)
    assert calc.convexity == pytest.approx(5.2719784936, abs=1e-7)
    assert calc.dv01 == pytest.approx(dv01(GC3_BOND, SETTLE, 13.63))
    assert calc.position_dv01 == pytest.approx(calc.dv01 * 10_000)
    assert (calc.previous_coupon, calc.next_coupon) == (date(2026, 8, 13), date(2027, 2, 13))
    assert calc.dirty_price - calc.clean_price == pytest.approx(4.325 * 48 / 184)
    inv = calc.invoice
    assert inv.days_accrued == 48
    assert inv.principal == pytest.approx(calc.clean_price * 10_000)
    assert inv.accrued == pytest.approx(1_000_000 * 0.0865 / 2 * 48 / 184)
    assert inv.total == pytest.approx(inv.principal + inv.accrued)


def test_a_later_settlement_accrues_more():
    sec = Security(GC3_BOND.maturity, GC3_BOND)
    early = calculate(sec, SETTLE, 1_000_000, yield_pct=13.63)
    late = calculate(sec, date(2026, 11, 30), 1_000_000, yield_pct=13.63)
    assert late.invoice.days_accrued == early.invoice.days_accrued + 61
    assert late.invoice.accrued > early.invoice.accrued


def test_bills_price_on_act_364_with_no_accrued():
    maturity = date(2027, 6, 21)
    calc = calculate(Security(maturity), SETTLE, 5_000_000, yield_pct=8.565)
    assert calc.clean_price == calc.dirty_price == pytest.approx(bill_price(SETTLE, maturity, 8.565))
    assert calc.previous_coupon is None and calc.macaulay_duration is None
    assert calc.invoice.accrued == 0 and calc.invoice.total == calc.invoice.principal
    assert calc.convexity > 0
    assert calculate(Security(maturity), SETTLE, 5_000_000, price=calc.clean_price).yield_pct == pytest.approx(8.565)


@pytest.mark.parametrize("kwargs", [
    dict(),                                  # neither
    dict(price=95.0, yield_pct=12.0),        # both
    dict(price=0.0),
    dict(price=-1.0),
])
def test_bad_inputs_are_refused(kwargs):
    with pytest.raises(ValueError):
        calculate(Security(GC3_BOND.maturity, GC3_BOND), SETTLE, 1_000_000, **kwargs)


def test_bad_face_or_settlement_is_refused():
    sec = Security(GC3_BOND.maturity, GC3_BOND)
    with pytest.raises(ValueError):
        calculate(sec, SETTLE, 0, yield_pct=12.0)
    with pytest.raises(ValueError):
        calculate(sec, GC3_BOND.maturity, 1_000_000, yield_pct=12.0)


# ---------------------------------------------------------------- resolving a quote

def test_security_for_bonds_bills_and_corporates():
    assert security_for(_tick(GC3), INSTRUMENTS[GC3]) == Security(GC3_BOND.maturity, GC3_BOND)
    # a mock-rolled bill isn't in the instrument master; its quote is enough
    bill = _tick("GHMK00000001", segment="treasury_bill", maturity_date=date(2027, 1, 4))
    assert security_for(bill, None) == Security(date(2027, 1, 4))
    with pytest.raises(ValueError, match="corporate"):
        security_for(_tick(LETSHEGO, segment="corporate"), INSTRUMENTS[LETSHEGO])
    with pytest.raises(ValueError, match="instrument master"):
        security_for(_tick("GHUNKNOWN001"), None)


def test_the_calculator_starts_from_the_close_else_the_mid():
    assert quoted_yield(_tick(GC3, closing_yield=13.6, bid_yield=13.7, ask_yield=13.5)) == 13.6
    assert quoted_yield(_tick(GC3, bid_yield=13.7, ask_yield=13.5)) == pytest.approx(13.6)
    assert quoted_yield(_tick(GC3, ask_yield=13.5)) == 13.5
    assert quoted_yield(_tick(GC3)) is None


# ---------------------------------------------------------------- the API

def test_analytics_api_defaults_to_the_close_at_t_plus_2(client):
    tick = client.get(f"/market/{GC3}").json()
    body = client.get(f"/bond/{GC3}/analytics").json()
    assert body["kind"] == "bond" and body["coupon"] == 8.65 and body["day_count"] == "ACT/ACT"
    assert body["settlement_date"] == body["default_settlement_date"] > body["trade_date"]
    assert body["solved_for"] == "price" and body["face"] == 1_000_000
    # the close may have moved since the /market read; it is a yield near it
    assert body["yield"] == pytest.approx(tick["closing_yield"], abs=1.0)
    settle = date.fromisoformat(body["settlement_date"])
    assert body["clean_price"] == pytest.approx(clean_price(GC3_BOND, settle, body["yield"]))
    assert body["invoice"]["total"] == pytest.approx(body["invoice"]["principal"] + body["invoice"]["accrued"])
    assert body["risk"]["position_dv01"] == pytest.approx(body["risk"]["dv01"] * 10_000)


def test_analytics_api_takes_price_or_yield_settlement_and_face(client):
    params = {"settle": "2026-09-30", "face": 2_000_000, "yield": 13.63}
    by_yield = client.get(f"/bond/{GC3}/analytics", params=params).json()
    assert by_yield["clean_price"] == pytest.approx(90.1682271075, abs=1e-8)
    assert by_yield["invoice"]["days_accrued"] == 48
    assert by_yield["invoice"]["principal"] == pytest.approx(by_yield["clean_price"] * 20_000)

    params = {"settle": "2026-09-30", "price": by_yield["clean_price"]}
    by_price = client.get(f"/bond/{GC3}/analytics", params=params).json()
    assert by_price["solved_for"] == "yield" and by_price["yield"] == pytest.approx(13.63, abs=1e-8)


def test_analytics_api_errors(client):
    assert client.get(f"/bond/{LETSHEGO}/analytics").status_code == 422      # corporates: no term sheets
    assert client.get("/bond/MTNGH/analytics").status_code == 404            # an equity
    assert client.get("/bond/NOPE/analytics").status_code == 404
    url = f"/bond/{GC3}/analytics"
    assert client.get(url, params={"price": 95, "yield": 12}).status_code == 400
    assert client.get(url, params={"settle": "2029-02-13"}).status_code == 400   # at maturity
    assert client.get(url, params={"face": 0}).status_code == 422
    assert client.get(url, params={"price": -5}).status_code == 422
