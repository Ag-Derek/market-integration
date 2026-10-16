"""Cross rates and synthetic forwards: the maths (app/fx/forwards.py),
the overnight benchmarks behind the foreign leg (app/fx/overnight.py:
parsing replies shaped like the NY Fed's, ECB's and BoE's, never the
network; storage; refresh), and /fx/rates' previous-fix fields,
/fx/cross, /fx/forwards and the /fx page."""

import json
from datetime import date, timedelta

import pytest

from app.auctions import AuctionResult
from app.auctions.store import AuctionStore
from app.auctions.tracker import AuctionTracker
from app.fx import Fix
from app.fx.forwards import GHS_BASIS, TENORS, cross_matrix, forward_curve, interpolate, synthetic_forward
from app.fx.overnight import (
    BENCHMARKS,
    OvernightError,
    OvernightRate,
    OvernightRates,
    parse_estr,
    parse_sofr,
    parse_sonia,
)
from app.fx.rates import FxRates
from app.fx.store import FxStore, OvernightStore

# ---------------------------------------------------------------- cross rates

def test_cross_matrix_is_consistent():
    ghs_per_unit = [1.0, 11.765, 13.71, 15.62, 0.1043]  # GHS, USD, EUR, GBP, NGN
    m = cross_matrix(ghs_per_unit)
    n = len(ghs_per_unit)
    for i in range(n):
        assert m[i][i] == 1
        for j in range(n):
            assert m[i][j] * m[j][i] == pytest.approx(1)
            for k in range(n):
                assert m[i][j] * m[j][k] == pytest.approx(m[i][k])
    assert m[1][0] == 11.765                          # 1 USD in cedis
    assert m[2][1] == pytest.approx(13.71 / 11.765)   # EURUSD


# ---------------------------------------------------------------- forwards

CURVE = [(91, 10.0), (182, 12.0), (364, 14.0)]


def test_interpolate_is_linear_between_points_and_flat_beyond():
    assert interpolate(CURVE, 7) == 10.0
    assert interpolate(CURVE, 91) == 10.0
    assert interpolate(CURVE, 136.5) == pytest.approx(11.0)
    assert interpolate(CURVE, 273) == pytest.approx(13.0)
    assert interpolate(CURVE, 365) == 14.0
    assert interpolate([(182, 12.0), (91, 10.0)], 136.5) == pytest.approx(11.0)  # any order
    with pytest.raises(ValueError):
        interpolate([], 30)


def test_synthetic_forward_is_covered_interest_parity():
    # 91 days, cedi 10% ACT/364, dollar 4% ACT/360.
    assert synthetic_forward(12.0, 91, 10.0, 4.0, 360) == pytest.approx(
        12.0 * (1 + 0.10 * 91 / 364) / (1 + 0.04 * 91 / 360))
    # Same rate, same day count: no carry, forward = spot.
    assert synthetic_forward(12.0, 180, 5.0, 5.0 * 360 / GHS_BASIS, 360) == pytest.approx(12.0)
    # Cedi rates above the foreign rate: the cedi trades at a forward discount.
    assert synthetic_forward(12.0, 91, 15.0, 4.0, 360) > 12.0


def test_forward_curve():
    spot_date = date(2026, 1, 31)
    curve = forward_curve(12.0, spot_date, CURVE, 4.0, 360)
    assert [p.tenor for p in curve] == [t[0] for t in TENORS]
    by = {p.tenor: p for p in curve}
    assert by["1W"].value_date == date(2026, 2, 7) and by["1W"].days == 7
    assert by["1M"].value_date == date(2026, 2, 28)   # clamped to the month end
    assert by["1Y"].value_date == date(2027, 1, 31) and by["1Y"].days == 365
    three = by["3M"]
    assert three.days == (date(2026, 4, 30) - spot_date).days == 89
    assert three.ghs_rate == 10.0                      # under 91 days: flat
    assert three.outright == pytest.approx(synthetic_forward(12.0, 89, 10.0, 4.0, 360))
    assert three.points == pytest.approx((three.outright - 12.0) * 10_000)
    assert three.premium_pa == pytest.approx((three.outright / 12.0 - 1) * 365 / 89 * 100)
    assert by["6M"].ghs_rate == pytest.approx(interpolate(CURVE, by["6M"].days))
    # A steeper cedi curve: the outrights rise with tenor.
    outrights = [p.outright for p in curve]
    assert outrights == sorted(outrights)


# ---------------------------------------------------------------- overnight benchmarks

SOFR_REPLY = json.dumps({"refRates": [
    {"effectiveDate": "2026-10-14", "type": "SOFR", "percentRate": 3.88, "volumeInBillions": 2974,
     "revisionIndicator": ""},
    {"effectiveDate": "2026-10-13", "type": "SOFR", "percentRate": 3.89, "volumeInBillions": 3000,
     "revisionIndicator": ""},
]}).encode()

ESTR_REPLY = (
    "KEY,FREQ,BENCHMARK_ITEM,DATA_TYPE_EST,TIME_PERIOD,OBS_VALUE,OBS_STATUS,TITLE\r\n"
    "EST.B.EU000A2X2A25.WT,B,EU000A2X2A25,WT,2026-10-14,2.44,A,Euro short-term rate\r\n"
    "EST.B.EU000A2X2A25.WT,B,EU000A2X2A25,WT,2026-10-13,2.439,A,Euro short-term rate\r\n"
).encode()

SONIA_REPLY = b"DATE,IUDSOIA\r\n13 Oct 2026,3.7319\r\n14 Oct 2026,3.7324\r\n"


def test_parsers_read_each_publishers_format_oldest_first():
    assert parse_sofr(SOFR_REPLY) == [OvernightRate("USD", date(2026, 10, 13), 3.89),
                                      OvernightRate("USD", date(2026, 10, 14), 3.88)]
    assert parse_estr(ESTR_REPLY) == [OvernightRate("EUR", date(2026, 10, 13), 2.439),
                                      OvernightRate("EUR", date(2026, 10, 14), 2.44)]
    assert parse_sonia(SONIA_REPLY) == [OvernightRate("GBP", date(2026, 10, 13), 3.7319),
                                        OvernightRate("GBP", date(2026, 10, 14), 3.7324)]
    # A negative €STR is a real rate (2019-2022), not an error.
    assert parse_estr(ESTR_REPLY.replace(b",2.44,", b",-0.5,"))[-1].rate == -0.5


@pytest.mark.parametrize("parse,body", [
    (parse_sofr, b"<html>maintenance</html>"),
    (parse_sofr, b'{"refRates": []}'),
    (parse_sofr, SOFR_REPLY.replace(b"3.88", b"388")),          # out of range
    (parse_estr, b""),
    (parse_estr, b"KEY,TIME_PERIOD,OBS_VALUE\r\nx,2026-13-01,2.4\r\n"),
    (parse_sonia, b"DATE,IUDSOIA\r\n"),
    (parse_sonia, b"DATE,IUDSOIA\r\n14/10/2026,3.73\r\n"),
])
def test_parsers_refuse_what_they_cant_read(parse, body):
    with pytest.raises(OvernightError):
        parse(body)


class FakeSource:
    def __init__(self, rates, failing=()):
        self.rates, self.failing = rates, set(failing)

    def fetch(self, currency, today):
        if currency in self.failing:
            raise OvernightError(f"{currency} is down")
        return [r for r in self.rates if r.currency == currency]


async def test_refresh_pulls_each_benchmark_on_its_own(tmp_path):
    rates = parse_sofr(SOFR_REPLY) + parse_estr(ESTR_REPLY) + parse_sonia(SONIA_REPLY)
    tracker = OvernightRates(OvernightStore(tmp_path / "o.db"), FakeSource(rates, failing={"EUR"}))
    assert await tracker.refresh(date(2026, 10, 16)) is False
    assert tracker.latest("USD").rate == 3.88 and tracker.latest("GBP").rate == 3.7324
    assert tracker.latest("EUR") is None
    status = tracker.describe()
    assert "EUR is down" in status["€STR"]["last_error"] and status["SOFR"]["last_error"] is None
    assert status["SOFR"]["latest"] == "2026-10-14"
    # The ECB comes back: what's stored stays, the missing one fills in.
    tracker._source = FakeSource(rates)
    assert await tracker.refresh(date(2026, 10, 16)) is True
    assert tracker.latest("EUR").rate == 2.44 and tracker.describe()["€STR"]["last_error"] is None


def test_store_keeps_rates_and_takes_revisions(tmp_path):
    db = tmp_path / "o.db"
    OvernightStore(db).upsert(parse_sofr(SOFR_REPLY))
    OvernightStore(db).upsert([OvernightRate("USD", date(2026, 10, 14), 3.87)])  # revised
    tracker = OvernightRates(OvernightStore(db), FakeSource([]))
    tracker.reload()
    assert tracker.latest("USD").rate == 3.87
    assert tracker.on("USD", date(2026, 10, 13)).rate == 3.89
    assert tracker.on("USD", date(2026, 10, 20)).rate == 3.87   # a later day: the latest before it
    assert tracker.on("USD", date(2026, 10, 1)) is None         # before what's held


# ---------------------------------------------------------------- API
# `client` is the session-wide app. These swap their own rates into its
# trackers' indexes for one test at a time (monkeypatch puts them back),
# so they neither depend on nor disturb what other modules stored.

SPOT_DAY = date(2026, 10, 15)    # a Thursday
PREV_DAY = date(2026, 10, 14)
MIDS = {"USDGHS": (11.70, 11.765), "EURGHS": (13.60, 13.71), "GBPGHS": (15.50, 15.62), "GHSNGN": (113.0, 112.95)}
BILLS = {"91 DAY BILL": 10.0, "182 DAY BILL": 12.0, "364 DAY BILL": 14.0}
OVERNIGHT = [OvernightRate("USD", date(2026, 10, 14), 3.88), OvernightRate("EUR", date(2026, 10, 15), 2.44)]


@pytest.fixture
def fx_world(client, tmp_path, monkeypatch):
    from app import main

    fx = FxRates(FxStore(tmp_path / "fx.db"), source=None)
    fx._store.upsert([Fix(d, pair, pair, mid * 0.999, mid * 1.001, mid)
                      for pair, mids in MIDS.items() for d, mid in zip((PREV_DAY, SPOT_DAY), mids)])
    fx.reload()
    monkeypatch.setattr(main.fx_rates, "_index", fx._index)

    bills = AuctionTracker(AuctionStore(tmp_path / "a.db"), source=None)
    bills._store.upsert([AuctionResult(date(2026, 10, 12), 2028, s, r - 0.5, r) for s, r in BILLS.items()])
    bills.reload()
    monkeypatch.setattr(main.auctions, "_index", bills._index)

    overnight = OvernightRates(OvernightStore(tmp_path / "o.db"), FakeSource([]))
    overnight._store.upsert(OVERNIGHT)
    overnight.reload()
    monkeypatch.setattr(main.overnight_rates, "_index", overnight._index)
    return main


def test_rates_carry_the_previous_fix_and_the_move(client, fx_world):
    rows = {r["code"]: r for r in client.get("/fx/rates").json()["currencies"]}
    usd = rows["USD"]
    assert usd["previous_date"] == PREV_DAY.isoformat() and usd["previous_mid"] == 11.70
    assert usd["change_percent"] == pytest.approx((11.765 / 11.70 - 1) * 100)
    # NGN is quoted naira per cedi; the change is in cedis per naira, like the rest.
    assert rows["NGN"]["change_percent"] == pytest.approx((113.0 / 112.95 - 1) * 100)
    # The first fix held has nothing before it.
    first = {r["code"]: r for r in client.get(f"/fx/rates?date={PREV_DAY}").json()["currencies"]}
    assert first["USD"]["previous_mid"] is None and first["USD"]["change_percent"] is None


def test_cross_defaults_to_the_cedi_and_every_currency(client, fx_world):
    body = client.get("/fx/cross").json()
    codes = [c["code"] for c in body["currencies"]]
    assert codes == ["GHS", "USD", "EUR", "GBP", "NGN"]
    m = body["matrix"]
    assert m[1][0] == pytest.approx(11.765)                    # USD -> GHS
    assert m[0][1] == pytest.approx(1 / 11.765)                # GHS -> USD
    assert m[2][1] == pytest.approx(13.71 / 11.765)            # EUR -> USD
    assert m[0][4] == pytest.approx(112.95)                    # GHS -> NGN, as BoG quotes it
    assert body["as_of"] == SPOT_DAY.isoformat() and body["currencies"][0]["name"] == "Ghanaian cedi"


def test_cross_for_chosen_currencies_and_a_date(client, fx_world):
    body = client.get(f"/fx/cross?currencies=eur,usd,EUR&date={PREV_DAY}").json()
    assert [c["code"] for c in body["currencies"]] == ["EUR", "USD"]   # deduplicated, in the order given
    assert body["matrix"][0][1] == pytest.approx(13.60 / 11.70)
    assert body["fixes"] == {"EUR": PREV_DAY.isoformat(), "USD": PREV_DAY.isoformat()}


@pytest.mark.parametrize("query,status", [
    ("currencies=USD,XYZ", 400),
    ("currencies=USD", 400),
    ("currencies=GHS,USD&date=1990-01-01", 404),
])
def test_cross_errors(client, fx_world, query, status):
    assert client.get(f"/fx/cross?{query}").status_code == status


def test_forwards(client, fx_world):
    body = client.get("/fx/forwards").json()
    assert [p["pair"] for p in body["pairs"]] == ["USDGHS", "EURGHS"]
    assert body["unavailable"] == {"GBP": "No SONIA rate yet"}
    assert [(p["tenor_days"], p["rate"]) for p in body["cedi_curve"]["points"]] == [(91, 10.0), (182, 12.0), (364, 14.0)]
    usd = body["pairs"][0]
    assert usd["spot"] == 11.765 and usd["spot_date"] == SPOT_DAY.isoformat()
    assert usd["foreign_rate"]["benchmark"] == "SOFR" and usd["foreign_rate"]["day_count"] == "ACT/360"
    three = next(t for t in usd["tenors"] if t["tenor"] == "3M")
    assert three["value_date"] == "2027-01-15" and three["days"] == 92
    ghs_rate = 10.0 + 2.0 * (92 - 91) / (182 - 91)
    assert three["ghs_rate"] == pytest.approx(ghs_rate)
    assert three["outright"] == pytest.approx(11.765 * (1 + ghs_rate / 100 * 92 / 364) / (1 + 0.0388 * 92 / 360))
    eur = body["pairs"][1]
    assert eur["foreign_rate"]["rate"] == 2.44 and eur["tenors"][0]["foreign_rate"] == 2.44
    assert set(body["status"]) == {b.name for b in BENCHMARKS.values()}


def test_forwards_need_the_cedi_yields(client, fx_world, monkeypatch):
    monkeypatch.setattr(fx_world.auctions, "_index", {})
    res = client.get("/fx/forwards")
    assert res.status_code == 503 and "T-bill" in res.json()["detail"]["message"]


def test_fx_page_and_health(client):
    page = client.get("/fx")
    assert page.status_code == 200 and "Synthetic forwards" in page.text and "Cross rates" in page.text
    health = client.get("/health").json()
    assert set(health["overnight_rates"]) == {"SOFR", "€STR", "SONIA"}
