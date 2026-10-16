from datetime import date, datetime, timezone

import pytest

from app.analytics.spreads import MIN_FIT_POINTS, corporate_yield, fit_svensson, issuer_spreads, spread_analysis
from app.bond_math import Bond, clean_price
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.models.fixed_income import CorporateSpread, CurveInstrument, CurvePoint, GovernmentYieldCurve
from app.models.instrument import Instrument

DAY = date(2026, 9, 28)


def _svensson(t: float) -> float:
    """A known curve with a V inside a year, like the GHS bills."""
    import math
    x1, x2 = t / 0.1, t / 3.0
    s = (1 - math.exp(-x1)) / x1
    return 15 - 5 * s - 12 * (s - math.exp(-x1)) + 2 * ((1 - math.exp(-x2)) / x2 - math.exp(-x2))


def _point(days: int, y: float, segment: str = "treasury_bill", symbol: str = None) -> CurvePoint:
    return CurvePoint(
        tenor_years=round(days / 365.25, 4), days_to_maturity=days, yield_=y, segment=segment,
        maturity_date=date.fromordinal(DAY.toordinal() + days),
        instruments=[CurveInstrument(symbol=symbol or f"B{days}", description=f"bill {days}", tenor="")],
    )


def _curve(points) -> GovernmentYieldCurve:
    return GovernmentYieldCurve(date=DAY, as_of=datetime(2026, 9, 28, 16, tzinfo=timezone.utc), points=points)


def _corp(symbol="C1", coupon=20.0, maturity=date(2029, 9, 28), issuer="Acme PLC") -> Instrument:
    return Instrument(symbol=symbol, name=f"{symbol}-BD", asset_class="bond", sector="Financial Services",
                      currency="GHS", status="active", issuer=issuer, segment="corporate",
                      tenor="BOND", maturity_date=maturity, coupon_rate=coupon, face_value=100)


# ---------------------------------------------------------------- the fit

def test_the_fit_recovers_a_curve_that_bends_inside_a_year():
    days = list(range(7, 365, 7)) + [730, 1095, 1826, 2557, 3652]
    fit = fit_svensson([(d / 365.25, _svensson(d / 365.25)) for d in days])
    assert fit.rmse * 100 < 5  # bp
    assert fit.decays[0] < fit.decays[1]


def test_too_few_points_to_fit():
    with pytest.raises(ValueError):
        fit_svensson([(1.0, 10.0)] * (MIN_FIT_POINTS - 1))
    a = spread_analysis(_curve([_point(91, 10.0)]), [])
    assert a.curve is None and a.government == [] and a.corporate == []


def test_government_spreads_are_residuals_and_old_gog_is_measured_but_not_fitted():
    days = list(range(7, 365, 14)) + [730, 1460, 2920]
    points = [_point(d, round(_svensson(d / 365.25), 4)) for d in days]
    points.append(_point(1000, _svensson(1000 / 365.25) + 3.0, "old_gog", "OLD"))   # 300bp cheap, stale
    a = spread_analysis(_curve(points), [])

    old = next(g for g in a.government if g.symbol == "OLD")
    assert not old.in_fit and old.spread_bp == pytest.approx(300, abs=10)
    # The stale bond didn't drag the curve: the bills still sit on it.
    assert all(abs(g.spread_bp) < 10 for g in a.government if g.in_fit)
    assert set(a.curve.segment_rmse_bp) == {"treasury_bill"}
    for g in a.government:
        assert g.spread_bp == pytest.approx((g.yield_ - g.fitted_yield) * 100, abs=0.1)
    # The drawn line spans the fitted points only.
    assert a.curve.samples[0][0] == pytest.approx(7 / 365.25, abs=1e-3)
    assert a.curve.samples[-1][0] == pytest.approx(2920 / 365.25, abs=1e-3)


# ---------------------------------------------------------------- corporates

def test_corporate_yield_is_the_semi_annual_bullet_yield_of_its_price():
    inst = _corp(coupon=20.0)
    price = clean_price(Bond(inst.maturity_date, 20.0), DAY, 25.0)
    y, reason = corporate_yield(inst, price, DAY)
    assert reason is None and y == pytest.approx(25.0, abs=1e-8)


def test_corporate_without_a_coupon_says_why():
    y, reason = corporate_yield(_corp(coupon=None), 110.0, DAY)
    assert y is None and "coupon" in reason


def test_corporate_spread_is_over_the_fitted_curve_at_its_tenor():
    days = list(range(7, 365, 14)) + [730, 1460, 2920]
    curve = _curve([_point(d, round(_svensson(d / 365.25), 4)) for d in days])
    inst = _corp(maturity=date(2029, 9, 28))
    fair = _svensson((inst.maturity_date - DAY).days / 365.25)
    price = clean_price(Bond(inst.maturity_date, 20.0), DAY, fair + 4.0)
    matured = _corp("OLD", maturity=DAY)
    a = spread_analysis(curve, [(inst, price), (_corp("NC", coupon=None), 101.0), (matured, 100.0)])

    assert [c.symbol for c in a.corporate] == ["C1", "NC"]  # matured left out; by tenor
    c1 = a.corporate[0]
    assert c1.yield_ == pytest.approx(fair + 4.0, abs=1e-3)
    assert c1.spread_bp == pytest.approx((c1.yield_ - c1.fitted_yield) * 100, abs=0.1)
    assert c1.spread_bp == pytest.approx(400, abs=15)  # what's left is the fit's own error at 3y
    assert a.corporate[1].spread_bp is None and a.corporate[1].reason
    assert [(i.issuer, i.bonds) for i in a.issuers] == [("Acme PLC", 1)]


def test_issuer_spreads_are_widest_first_and_skip_missing():
    def row(sym, issuer, s):
        return CorporateSpread(symbol=sym, description=sym, issuer=issuer, maturity_date=DAY, tenor_years=1.0,
                               clean_price=100.0, fitted_yield=10.0, spread_bp=s)
    out = issuer_spreads([row("a", "A", 100.0), row("b", "A", 300.0), row("c", "B", 50.0), row("d", "B", None)])
    assert [(i.issuer, i.bonds, i.mean_spread_bp, i.min_spread_bp, i.max_spread_bp) for i in out] == [
        ("A", 2, 200.0, 100.0, 300.0), ("B", 1, 50.0, 50.0, 50.0),
    ]


# ---------------------------------------------------------------- on the mock market

def test_on_the_sample_the_bills_fit_tightly():
    market = MockFixedIncomeMarket(clock=lambda: datetime(2026, 9, 28, tzinfo=timezone.utc))
    market.start()
    day, closes = market.corporate_closes()
    a = spread_analysis(market.curve(), closes)

    assert day == a.date == DAY
    # The sample's bills run 9.4% -> 4.7% -> 9.8% inside a year; Svensson follows that.
    assert a.curve.segment_rmse_bp["treasury_bill"] < 50
    # 22 corporates, 8 unpriced; the rest are on the analysis, three without a coupon.
    assert len(a.corporate) == 14
    assert sum(c.spread_bp is None for c in a.corporate) == 1  # LGH 04/10/29 (the other two are unpriced)
    quantum = next(c for c in a.corporate if c.issuer == "Quantum")
    assert quantum.clean_price == 40.9633 and quantum.spread_bp > 5000  # distressed
    assert a.issuers[0].issuer == "Quantum"


def test_spreads_api(client):
    body = client.get("/spreads").json()
    assert body["curve"]["method"] == "nelson_siegel_svensson"
    assert body["government"] and body["assumptions"]
    g = body["government"][0]
    assert {"symbol", "yield", "fitted_yield", "spread_bp", "in_fit"} <= set(g)
    assert client.get("/spreads", params={"date": body["date"]}).json()["date"] == body["date"]
    assert client.get("/spreads", params={"date": "2999-01-01"}).status_code == 400
