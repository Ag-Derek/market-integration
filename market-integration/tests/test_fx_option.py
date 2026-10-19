"""The FX option calculator: pricing (app/derivatives/fx_option.py)
checked against the closed-form Garman-Kohlhagen formula and the
relationships any option price must satisfy, historical volatility, and
POST /calc/fx-option with its defaults, overrides and errors."""

import math
import statistics
from datetime import date, timedelta

import pytest

from app.auctions import AuctionResult
from app.auctions.store import AuctionStore
from app.auctions.tracker import AuctionTracker
from app.derivatives.fx_option import (
    FIXES_PER_YEAR,
    Market,
    OptionError,
    forward,
    historical_vol,
    implied_vol,
    likely_range,
    value,
    values_across,
    window_for,
)
from app.fx import Fix
from app.fx.forwards import synthetic_forward
from app.fx.overnight import OvernightRate, OvernightRates
from app.fx.rates import FxRates
from app.fx.store import FxStore, OvernightStore

TODAY = date(2026, 10, 15)
EXPIRY = date(2027, 1, 15)           # 92 days
MARKET = Market(spot=11.65, vol=12.0, ghs_rate=4.62, foreign_rate=3.88, foreign_basis=360)


def _n(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def garman_kohlhagen(kind, spot, strike, days, vol, ghs_rate, foreign_rate, basis):
    """The textbook formula, written out independently of QuantLib, on the
    same simple-rate discount factors."""
    d_ghs = 1 / (1 + ghs_rate / 100 * days / 364)
    d_for = 1 / (1 + foreign_rate / 100 * days / basis)
    fwd = spot * d_for / d_ghs
    t = days / 365
    s = vol / 100 * math.sqrt(t)
    d1 = (math.log(fwd / strike) + s * s / 2) / s
    d2 = d1 - s
    if kind == "call":
        return d_ghs * (fwd * _n(d1) - strike * _n(d2))
    return d_ghs * (strike * _n(-d2) - fwd * _n(-d1))


# ---------------------------------------------------------------- pricing

@pytest.mark.parametrize("kind", ["call", "put"])
@pytest.mark.parametrize("strike", [11.0, 11.65, 11.6704, 12.5])
def test_premium_matches_the_closed_form(kind, strike):
    v = value(kind, strike, TODAY, EXPIRY, MARKET)
    assert v.premium == pytest.approx(garman_kohlhagen(kind, 11.65, strike, 92, 12.0, 4.62, 3.88, 360), rel=1e-9)


def test_the_forward_is_the_fx_pages_forward():
    assert forward(TODAY, EXPIRY, MARKET) == pytest.approx(synthetic_forward(11.65, 92, 4.62, 3.88, 360))
    assert value("call", 11.7, TODAY, EXPIRY, MARKET).forward == forward(TODAY, EXPIRY, MARKET)


def test_put_call_parity():
    for strike in (11.0, 11.7, 12.4):
        c = value("call", strike, TODAY, EXPIRY, MARKET)
        p = value("put", strike, TODAY, EXPIRY, MARKET)
        d_ghs = 1 / (1 + 0.0462 * 92 / 364)
        assert c.premium - p.premium == pytest.approx((c.forward - strike) * d_ghs, abs=1e-12)


def test_greeks_agree_with_small_bumps():
    v = value("call", 11.7, TODAY, EXPIRY, MARKET)
    h = 1e-3
    down, mid, up = values_across("call", 11.7, TODAY, EXPIRY, MARKET, [11.65 - h, 11.65, 11.65 + h])
    assert mid == pytest.approx(v.premium)
    assert v.delta == pytest.approx((up - down) / (2 * h), rel=1e-5)
    assert v.gamma == pytest.approx((up - 2 * mid + down) / h ** 2, rel=1e-3)
    bump = lambda **kw: value("call", 11.7, TODAY, EXPIRY, Market(**{**MARKET.__dict__, **kw})).premium  # noqa: E731
    assert v.vega == pytest.approx((bump(vol=12.01) - bump(vol=11.99)) / 0.02, rel=1e-3)   # per vol point
    assert v.theta < 0 and v.vega > 0 and v.rho_ghs > 0 and v.rho_foreign < 0
    later = value("call", 11.7, TODAY + timedelta(days=1), EXPIRY, MARKET).premium
    assert v.theta == pytest.approx(later - v.premium, rel=0.05)                          # per day
    p = value("put", 11.7, TODAY, EXPIRY, MARKET)
    assert v.delta - p.delta == pytest.approx(1 / (1 + 0.0388 * 92 / 360))               # e^{-r_f T}


def test_implied_vol_round_trips():
    for kind, strike in (("call", 11.7), ("put", 11.2), ("call", 12.6)):
        premium = value(kind, strike, TODAY, EXPIRY, MARKET).premium
        assert implied_vol(kind, strike, TODAY, EXPIRY, MARKET, premium) == pytest.approx(12.0, abs=1e-5)


def test_implied_vol_refuses_a_price_no_vol_explains():
    floor = value("call", 11.0, TODAY, EXPIRY, Market(**{**MARKET.__dict__, "vol": 0.01})).premium
    with pytest.raises(OptionError, match="no uncertainty"):
        implied_vol("call", 11.0, TODAY, EXPIRY, MARKET, floor * 0.9)
    with pytest.raises(OptionError):
        implied_vol("call", 11.7, TODAY, EXPIRY, MARKET, 50.0)   # more than the currency itself


@pytest.mark.parametrize("args", [
    ("call", 11.7, TODAY, TODAY, MARKET),
    ("call", 0, TODAY, EXPIRY, MARKET),
    ("call", 11.7, TODAY, EXPIRY, Market(11.65, 0, 4.62, 3.88, 360)),
])
def test_unpriceable_terms(args):
    with pytest.raises(OptionError):
        value(*args)


def test_historical_vol():
    mids = [11.0]
    for i in range(70):
        mids.append(mids[-1] * math.exp(0.004 if i % 3 else -0.006))
    changes = [math.log(b / a) for a, b in zip(mids, mids[1:])]
    assert historical_vol(mids, 21) == pytest.approx(statistics.stdev(changes[-21:]) * math.sqrt(FIXES_PER_YEAR) * 100)
    assert historical_vol(mids, 70) is not None and historical_vol(mids, 71) is None
    assert historical_vol([11.0] * 30, 21) == 0


def test_likely_range():
    fwd, vol, days = 11.67, 12.0, 92
    lo, hi = likely_range(fwd, vol, days)
    s = 0.12 * math.sqrt(92 / 365)
    mean = math.log(fwd) - s * s / 2          # the model's mean log rate: its average is the forward
    assert _n((math.log(lo) - mean) / s) == pytest.approx(0.05, abs=1e-4)
    assert _n((math.log(hi) - mean) / s) == pytest.approx(0.95, abs=1e-4)
    wider = likely_range(fwd, vol, 365)
    assert wider[0] < lo and wider[1] > hi    # more time, more room to move


def test_window_for():
    assert [window_for(d) for d in (7, 31, 61, 92, 182, 273, 365)] == ["1M", "1M", "3M", "3M", "6M", "6M", "1Y"]


# ---------------------------------------------------------------- API
# As in test_fx_forwards.py: each test swaps its own rates into the
# session app's trackers and monkeypatch puts the originals back.

def _mid(i):
    """A fix that wanders: up 0.2%, then down 0.1%, a fifth of the days flat."""
    return 11.0 * math.exp(sum((0.002 if k % 2 else -0.001) * (k % 5 != 0) for k in range(i)))


@pytest.fixture
def market(client, tmp_path, monkeypatch):
    from app import main

    days = []
    d = TODAY
    while len(days) < 300:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    fx = FxRates(FxStore(tmp_path / "fx.db"), source=None)
    fx._store.upsert([Fix(d, "US Dollar", "USDGHS", _mid(i) - 0.006, _mid(i) + 0.006, _mid(i))
                      for i, d in enumerate(days)] +
                     [Fix(TODAY, "Swiss Franc", "CHFGHS", 14.03, 14.05, 14.04)])
    fx.reload()
    monkeypatch.setattr(main.fx_rates, "_index", fx._index)

    bills = AuctionTracker(AuctionStore(tmp_path / "a.db"), source=None)
    bills._store.upsert([AuctionResult(date(2026, 10, 12), 2028, s, r - 0.2, r)
                         for s, r in (("91 DAY BILL", 4.60), ("182 DAY BILL", 6.28), ("364 DAY BILL", 9.75))])
    bills.reload()
    monkeypatch.setattr(main.auctions, "_index", bills._index)

    overnight = OvernightRates(OvernightStore(tmp_path / "o.db"), source=None)
    overnight._store.upsert([OvernightRate("USD", date(2026, 10, 14), 3.88)])
    overnight.reload()
    monkeypatch.setattr(main.overnight_rates, "_index", overnight._index)
    return main


def _calc(client, **body):
    return client.post("/calc/fx-option", json={"valuation_date": TODAY.isoformat(), **body})


def test_defaults_come_from_market_data(client, market):
    res = _calc(client, notional=1000)
    assert res.status_code == 200
    b = res.json()
    assert (b["pair"], b["kind"], b["expiry"], b["days"]) == ("USDGHS", "call", "2027-01-15", 92)
    inputs = {i["key"]: i for i in b["inputs"]}
    assert {k: i["source"] for k, i in inputs.items()} == {
        "spot": "market", "ghs_rate": "market", "foreign_rate": "market", "vol": "estimate"}
    spot = _mid(299)
    assert inputs["spot"]["value"] == pytest.approx(spot) and inputs["spot"]["as_of"] == TODAY.isoformat()
    ghs_rate = 4.60 + (6.28 - 4.60) * (92 - 91) / (182 - 91)
    assert inputs["ghs_rate"]["value"] == pytest.approx(ghs_rate)
    assert inputs["foreign_rate"]["value"] == 3.88 and "SOFR" in inputs["foreign_rate"]["note"]
    assert inputs["vol"]["value"] == pytest.approx(b["historical_vol"]["3M"]) and "3M" in inputs["vol"]["note"]
    m = Market(spot, inputs["vol"]["value"], ghs_rate, 3.88, 360)
    assert b["forward"] == pytest.approx(forward(TODAY, EXPIRY, m))
    assert b["strike"] == round(b["forward"], 4) and b["strike_source"] == "forward"
    v = value("call", b["strike"], TODAY, EXPIRY, m)
    assert b["premium"]["per_unit"] == pytest.approx(v.premium) and b["premium"]["total"] == pytest.approx(v.premium * 1000)
    assert b["breakeven"] == pytest.approx(b["strike"] + v.premium)
    assert b["greeks"]["vega_total"] == pytest.approx(v.vega * 1000)
    pl = b["profit_loss"]
    assert len(pl["spots"]) == len(pl["today"]) == len(pl["at_expiry"]) == 61
    # Out of the money at expiry: you lose the premium, no more.
    assert pl["at_expiry"][0] == pytest.approx(-b["premium"]["total"])
    assert min(pl["spots"]) < b["strike"] < max(pl["spots"])


def test_overrides_are_used_and_labelled(client, market):
    b = _calc(client, kind="put", notional=50_000, expiry="2026-12-15", strike=11.5, spot=12.0, vol=15,
              ghs_rate=8, foreign_rate=4).json()
    assert {i["key"]: i["source"] for i in b["inputs"]} == dict.fromkeys(("spot", "ghs_rate", "foreign_rate", "vol"), "you")
    assert b["strike_source"] == "you" and b["days"] == 61
    expected = garman_kohlhagen("put", 12.0, 11.5, 61, 15, 8, 4, 360)
    assert b["premium"]["total"] == pytest.approx(expected * 50_000, rel=1e-9)
    assert b["breakeven"] == pytest.approx(11.5 - expected)


def test_a_quoted_price_gives_its_volatility(client, market):
    priced = _calc(client, notional=1000, vol=12).json()
    b = _calc(client, notional=1000, quoted_premium=round(priced["premium"]["total"] * 2, 2)).json()
    assert b["implied_vol"]["vol"] > 12 and b["implied_vol"]["error"] is None
    same = _calc(client, notional=1000, quoted_premium=priced["premium"]["total"], vol=12).json()
    assert same["implied_vol"]["vol"] == pytest.approx(12, abs=1e-4)
    bad = _calc(client, notional=1000, strike=10, quoted_premium=1).json()   # far below its intrinsic value
    assert bad["implied_vol"]["vol"] is None and "no uncertainty" in bad["implied_vol"]["error"]


def test_a_currency_without_a_rate_needs_one_entered(client, market):
    res = _calc(client, currency="chf")
    assert res.status_code == 400 and "enter the CHF interest rate" in res.json()["detail"]
    # The CHF history is one fix: volatility has to be entered too.
    res = _calc(client, currency="CHF", foreign_rate=0.5)
    assert res.status_code == 400 and "volatility" in res.json()["detail"]
    b = _calc(client, currency="CHF", foreign_rate=0.5, vol=9).json()
    assert b["pair"] == "CHFGHS" and "ACT/365" in {i["key"]: i for i in b["inputs"]}["foreign_rate"]["unit"]


@pytest.mark.parametrize("body,status", [
    ({"currency": "XYZ"}, 400),
    ({"currency": "GHS"}, 400),
    ({"tenor": "5Y"}, 400),
    ({"expiry": "2026-10-01"}, 400),
    ({"notional": 0}, 422),
    ({"strike": -1}, 422),
    ({"surprise": 1}, 422),
])
def test_errors(client, market, body, status):
    assert _calc(client, **body).status_code == status


def test_the_likely_range_is_the_middle_90_percent(client, market):
    b = _calc(client, notional=1000).json()
    lo, hi = b["likely_range"]
    assert lo < b["forward"] < hi
    vol = {i["key"]: i for i in b["inputs"]}["vol"]["value"]
    assert (lo, hi) == pytest.approx(likely_range(b["forward"], vol, b["days"]))


def test_hedging_page_and_the_old_address(client):
    page = client.get("/hedging")
    assert page.status_code == 200 and "Plan a foreign-currency payment" in page.text
    old = client.get("/calculators", follow_redirects=False)
    assert old.status_code == 301 and old.headers["location"] == "/hedging"
