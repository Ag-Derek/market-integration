"""The FX swap calculator: pricing (app/derivatives/fx_swap.py) checked
against the synthetic forwards it is built on, and POST /calc/fx-swap
with its defaults, overrides and errors, against /fx/forwards."""

from datetime import date, timedelta

import pytest

from app.auctions import AuctionResult
from app.auctions.store import AuctionStore
from app.auctions.tracker import AuctionTracker
from app.derivatives.fx_swap import SwapError, price
from app.fx import Fix
from app.fx.forwards import PIP, TENORS, forward_curve, synthetic_forward, tenor_date
from app.fx.overnight import OvernightRate, OvernightRates
from app.fx.rates import FxRates
from app.fx.store import FxStore, OvernightStore

TODAY = date(2026, 10, 15)
SPOT = 11.65
CURVE = [(91, 4.60), (182, 6.28), (364, 9.75)]
SOFR = 3.88


def _swap(direction="buy_sell", near=TODAY, far=date(2027, 1, 15), notional=100_000):
    return price(direction, notional, SPOT, TODAY, near, far, CURVE, SOFR, 360)


# ---------------------------------------------------------------- pricing

def test_a_spot_start_swap_is_spot_then_the_forward():
    s = _swap()
    assert s.near.days == 0 and s.near.rate == SPOT and s.near.ghs_rate is None
    ghs = 4.60 + (6.28 - 4.60) * (92 - 91) / (182 - 91)
    assert s.far.days == 92 and s.far.ghs_rate == pytest.approx(ghs)
    assert s.far.rate == pytest.approx(synthetic_forward(SPOT, 92, ghs, SOFR, 360))
    assert s.points == pytest.approx((s.far.rate - SPOT) / PIP)
    assert s.premium_pa == pytest.approx((s.far.rate / SPOT - 1) * 365 / 92 * 100)
    # Buy/sell: receive the dollars and pay cedis now, the reverse later.
    assert (s.near.foreign_amount, s.far.foreign_amount) == (100_000, -100_000)
    assert s.near.ghs_amount == pytest.approx(-100_000 * SPOT)
    assert s.far.ghs_amount == pytest.approx(100_000 * s.far.rate)
    assert s.net_ghs == pytest.approx(100_000 * (s.far.rate - SPOT))


def test_a_forward_start_swap_prices_both_legs_off_the_forward_curve():
    curve = {p.tenor: p for p in forward_curve(SPOT, TODAY, CURVE, SOFR, 360)}
    s = _swap(near=curve["1M"].value_date, far=curve["6M"].value_date)
    assert s.near.rate == pytest.approx(curve["1M"].outright) and s.near.ghs_rate == pytest.approx(curve["1M"].ghs_rate)
    assert s.far.rate == pytest.approx(curve["6M"].outright) and s.far.ghs_rate == pytest.approx(curve["6M"].ghs_rate)
    assert s.points == pytest.approx(curve["6M"].points - curve["1M"].points)
    life = curve["6M"].days - curve["1M"].days
    assert s.premium_pa == pytest.approx((s.far.rate / s.near.rate - 1) * 365 / life * 100)


def test_direction_flips_every_amount_but_not_the_rates():
    a, b = _swap("buy_sell"), _swap("sell_buy")
    assert (a.near.rate, a.far.rate, a.points) == (b.near.rate, b.far.rate, b.points)
    for x, y in ((a.near, b.near), (a.far, b.far)):
        assert x.foreign_amount == -y.foreign_amount and x.ghs_amount == pytest.approx(-y.ghs_amount)
    assert a.net_ghs > 0 and b.net_ghs == pytest.approx(-a.net_ghs)


@pytest.mark.parametrize("kwargs, message", [
    ({"direction": "both"}, "direction"),
    ({"notional": 0}, "notional"),
    ({"near": TODAY - timedelta(days=1)}, "before the spot date"),
    ({"far": TODAY}, "after the near date"),
    ({"near": date(2026, 12, 1), "far": date(2026, 11, 1)}, "after the near date"),
])
def test_unpriceable_terms(kwargs, message):
    with pytest.raises(SwapError, match=message):
        _swap(**kwargs)


def test_tenor_dates_match_the_forward_curve():
    for p in forward_curve(SPOT, TODAY, CURVE, SOFR, 360):
        assert tenor_date(TODAY, p.tenor) == p.value_date
    with pytest.raises(ValueError, match="Unknown tenor"):
        tenor_date(TODAY, "5Y")


# ---------------------------------------------------------------- endpoint

@pytest.fixture
def market(client, tmp_path, monkeypatch):
    from app import main

    fx = FxRates(FxStore(tmp_path / "fx.db"), source=None)
    fx._store.upsert([Fix(TODAY - timedelta(days=1), "US Dollar", "USDGHS", 11.60, 11.62, 11.61),
                      Fix(TODAY, "US Dollar", "USDGHS", SPOT - 0.006, SPOT + 0.006, SPOT),
                      Fix(TODAY, "Swiss Franc", "CHFGHS", 14.03, 14.05, 14.04)])
    fx.reload()
    monkeypatch.setattr(main.fx_rates, "_index", fx._index)

    bills = AuctionTracker(AuctionStore(tmp_path / "a.db"), source=None)
    bills._store.upsert([AuctionResult(date(2026, 10, 12), 2028, s, r - 0.2, r)
                         for s, r in (("91 DAY BILL", 4.60), ("182 DAY BILL", 6.28), ("364 DAY BILL", 9.75))])
    bills.reload()
    monkeypatch.setattr(main.auctions, "_index", bills._index)

    overnight = OvernightRates(OvernightStore(tmp_path / "o.db"), source=None)
    overnight._store.upsert([OvernightRate("USD", date(2026, 10, 14), SOFR)])
    overnight.reload()
    monkeypatch.setattr(main.overnight_rates, "_index", overnight._index)
    return main


def _calc(client, **body):
    return client.post("/calc/fx-swap", json={"valuation_date": TODAY.isoformat(), **body})


def _forwards(client):
    pair = next(p for p in client.get("/fx/forwards").json()["pairs"] if p["currency"] == "USD")
    return {t["tenor"]: t for t in pair["tenors"]}


@pytest.mark.parametrize("tenor", [t[0] for t in TENORS])
def test_the_far_rate_is_the_fx_forwards_outright(client, market, tenor):
    fwd = _forwards(client)[tenor]
    b = _calc(client, far_tenor=tenor).json()
    assert b["far"]["value_date"] == fwd["value_date"] and b["far"]["days"] == fwd["days"]
    assert b["far"]["rate"] == pytest.approx(fwd["outright"], rel=1e-12)
    assert b["far"]["ghs_rate"] == pytest.approx(fwd["ghs_rate"])
    # The same date given directly prices the same.
    assert _calc(client, far_date=fwd["value_date"]).json()["far"]["rate"] == b["far"]["rate"]


def test_a_spot_start_swap_from_market_data(client, market):
    res = _calc(client, notional=250_000)
    assert res.status_code == 200
    b = res.json()
    assert (b["pair"], b["direction"], b["spot_date"], b["forward_start"]) == ("USDGHS", "buy_sell", "2026-10-15", False)
    assert (b["near"]["tenor"], b["near"]["value_date"], b["far"]["tenor"], b["far"]["value_date"]) == \
        ("SPOT", "2026-10-15", "3M", "2027-01-15")
    assert b["near"]["rate"] == SPOT == b["spot"]
    assert b["points"] == pytest.approx((b["far"]["rate"] - b["near"]["rate"]) / PIP)
    assert b["days"] == 92
    assert b["near"]["ghs_amount"] == pytest.approx(-250_000 * SPOT)
    assert b["net_ghs"] == pytest.approx(250_000 * (b["far"]["rate"] - SPOT))
    inputs = {i["key"]: i for i in b["inputs"]}
    assert {k: i["source"] for k, i in inputs.items()} == dict.fromkeys(("spot", "ghs_rate", "foreign_rate"), "market")
    assert inputs["spot"]["as_of"] == "2026-10-15" and inputs["spot"]["value"] == SPOT
    assert inputs["ghs_rate"]["as_of"] == "2026-10-12" and "read at 92 days" in inputs["ghs_rate"]["note"]
    assert inputs["foreign_rate"]["value"] == SOFR and "SOFR" in inputs["foreign_rate"]["note"]
    assert "not a dealer quote" in b["note"]


def test_a_forward_start_swap_agrees_with_the_forward_curve(client, market):
    fwd = _forwards(client)
    b = _calc(client, near_tenor="1M", far_tenor="3M").json()
    assert b["forward_start"] is True
    assert b["near"]["rate"] == pytest.approx(fwd["1M"]["outright"], rel=1e-12)
    assert b["far"]["rate"] == pytest.approx(fwd["3M"]["outright"], rel=1e-12)
    assert b["days"] == fwd["3M"]["days"] - fwd["1M"]["days"]
    assert b["points"] == pytest.approx(fwd["3M"]["points"] - fwd["1M"]["points"])
    inputs = {i["key"]: i for i in b["inputs"]}
    assert inputs["ghs_rate_near"]["value"] == pytest.approx(fwd["1M"]["ghs_rate"])
    assert inputs["ghs_rate"]["value"] == pytest.approx(fwd["3M"]["ghs_rate"])


def test_direction_flips_the_net_cedi_amount(client, market):
    a = _calc(client).json()
    b = _calc(client, direction="sell_buy").json()
    assert a["far"]["rate"] == b["far"]["rate"]
    assert b["net_ghs"] == pytest.approx(-a["net_ghs"]) and a["net_ghs"] > 0
    assert b["near"]["ghs_amount"] == pytest.approx(SPOT * 100_000) and b["far"]["foreign_amount"] == 100_000


def test_overrides_are_used_and_labelled(client, market):
    b = _calc(client, far_date="2026-12-15", spot=12.0, ghs_rate=8, foreign_rate=4).json()
    assert {i["key"]: i["source"] for i in b["inputs"]} == dict.fromkeys(("spot", "ghs_rate", "foreign_rate"), "you")
    assert b["near"]["rate"] == 12.0 and b["far"]["days"] == 61
    assert b["far"]["rate"] == pytest.approx(synthetic_forward(12.0, 61, 8, 4, 360), rel=1e-12)
    # One cedi rate entered serves both legs of a forward start.
    f = _calc(client, near_tenor="1M", far_tenor="6M", ghs_rate=8).json()
    assert f["near"]["ghs_rate"] == f["far"]["ghs_rate"] == 8
    assert "ghs_rate_near" not in {i["key"] for i in f["inputs"]}


def test_currencies_without_an_overnight_benchmark_are_rejected(client, market):
    for body in ({"currency": "chf"}, {"currency": "CHF", "foreign_rate": 0.5}):
        res = _calc(client, **body)
        assert res.status_code == 400 and "no overnight interest rate benchmark" in res.json()["detail"]


def test_a_missing_input_asks_for_it(client, market):
    res = _calc(client, currency="EUR")       # no BoG EUR fix held
    assert res.status_code == 400 and "enter the spot rate" in res.json()["detail"]
    res = _calc(client, currency="EUR", spot=13.5)  # nor €STR
    assert res.status_code == 400 and "enter the EUR interest rate" in res.json()["detail"]
    b = _calc(client, currency="EUR", spot=13.5, foreign_rate=2.0).json()
    assert b["pair"] == "EURGHS" and b["spot_date"] == TODAY.isoformat()


@pytest.mark.parametrize("body,status", [
    ({"currency": "XYZ"}, 400),
    ({"currency": "GHS"}, 400),
    ({"far_tenor": "5Y"}, 400),
    ({"near_tenor": "2W"}, 400),
    ({"near_date": "2026-10-01"}, 400),
    ({"near_tenor": "6M", "far_tenor": "3M"}, 400),
    ({"far_date": "2026-10-15"}, 400),
    ({"direction": "buy"}, 422),
    ({"notional": 0}, 422),
    ({"spot": -1}, 422),
    ({"surprise": 1}, 422),
])
def test_errors(client, market, body, status):
    assert _calc(client, **body).status_code == status


def test_the_hedging_page_has_the_swap_calculator(client):
    page = client.get("/hedging").text
    # The swap is its own sub-tab, as the Commodities page's views are.
    assert 'id="view-swap"' in page and 'href="#swap" data-view="swap"' in page
    assert 'id="view-payment"' in page and 'href="#payment" data-view="payment"' in page
    assert '"/calc/fx-swap"' in page and "Net cedi cash difference" in page
    assert "not a dealer quote" in page
