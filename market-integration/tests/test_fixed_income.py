from collections import Counter
from datetime import date, datetime, timezone

import pytest

from app.bond_math import Bond, clean_price
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.models.fixed_income import REPORT_SECTIONS, GovernmentBondQuote


class _Clock:
    def __init__(self, *args):
        self.now = datetime(*args, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def set(self, *args):
        self.now = datetime(*args, tzinfo=timezone.utc)


def _market(*when):
    clock = _Clock(*when)
    market = MockFixedIncomeMarket(clock=clock)
    market.start()
    return market, clock


def _row(rows, **match):
    return next(r for r in rows if all(getattr(r, k) == v for k, v in match.items()))


# ---------------------------------------------------------------- shape of the sample

def test_on_the_sample_date_before_any_trades_the_report_has_the_samples_shape():
    market, _ = _market(2026, 9, 28)  # midnight: nothing traded yet
    r = market.report()

    assert r.report_date == date(2026, 9, 28)
    assert {s: len(getattr(r, s)) for s in REPORT_SECTIONS} == {
        "new_gog": 2, "ddep": 29, "old_gog": 17, "treasury_bill": 91, "corporate": 22,
        # every New GoG and DDEP bond; the sample listed 30 of these 31
        "sell_buy_back": 31,
    }
    assert Counter(b.tenor for b in r.treasury_bill) == {"91-DAY BILL": 13, "182-DAY BILL": 26, "364-DAY BILL": 52}

    # Closing yields are the sample's; prices are derived from them with the
    # bond math, so they agree exactly (#35) -- the sample's own 90.1021 is
    # ~5bp of yield off (docs/fixed-income-sources-and-conventions.md).
    gc3 = _row(r.ddep, tenor="2023-GC-3")
    assert gc3.closing_yield == 13.63
    assert gc3.closing_price == round(clean_price(Bond(date(2029, 2, 13), 8.65), date(2026, 9, 28), 13.63), 4)
    # GFSF and USD DDE bonds don't price as bullets: they keep the sample's price.
    assert _row(r.ddep, tenor="GFSF-2-6YR").closing_price == 83.8288
    assert _row(r.ddep, tenor="USD-DDE-FEA-28").closing_price == 95.0565
    assert (gc3.day_low_yield, gc3.day_high_yield) == (12.87, 12.9203)  # carried over
    bill = _row(r.treasury_bill, symbol="GHGGOGI01883")
    assert (bill.days_to_maturity, bill.closing_yield, bill.closing_price) == (266, 8.5651, 94.1096)

    # Blanks where the sample had blanks.
    blank = _row(r.ddep, tenor="GFSF-7-5YR")
    assert blank.model_dump(include={
        "opening_yield", "closing_yield", "closing_price", "day_low_yield", "day_high_yield", "volume", "trade_count",
    }) == dict.fromkeys(("opening_yield", "closing_yield", "closing_price", "day_low_yield",
                         "day_high_yield", "volume", "trade_count"))
    assert sum(c.closing_price is None for c in r.corporate) == 8
    assert all(row.volume is None for s in REPORT_SECTIONS for row in getattr(r, s))
    assert r.summary.total_volume == 0 and r.summary.total_trade_count == 0

    # The newest bill of each tenor was issued today: no opening values.
    assert {(b.tenor, b.maturity_date.isoformat()) for b in r.treasury_bill if b.opening_yield is None} == {
        ("91-DAY BILL", "2026-12-28"), ("182-DAY BILL", "2027-03-29"), ("364-DAY BILL", "2027-09-27"),
    }
    # Bills of different tenors maturing the same day share one price.
    same_day = [b for b in r.treasury_bill if b.maturity_date == date(2026, 12, 28)]
    assert len(same_day) == 3 and len({b.closing_price for b in same_day}) == 1


def test_usd_bonds_keep_their_currency():
    market, _ = _market(2026, 9, 28)
    usd = [g for g in market.report().ddep if g.tenor.startswith("USD-")]
    assert len(usd) == 4 and {g.currency for g in usd} == {"USD"}


# ---------------------------------------------------------------- a trading day

def test_a_full_trading_day_looks_like_the_sample():
    market, clock = _market(2026, 9, 28)
    clock.set(2026, 9, 28, 23, 59)
    r = market.report()

    bills = r.summary.sections[REPORT_SECTIONS.index("treasury_bill")]
    assert bills.trade_count > 50 and bills.volume > 10_000_000  # sample: 239 trades, 830m
    # Most bonds don't trade on a given day.
    gov = r.new_gog + r.ddep + r.old_gog
    assert sum(g.volume is None for g in gov) > len(gov) / 3

    for section in r.summary.sections:
        rows = getattr(r, section.section)
        assert section.volume == sum(row.volume or 0 for row in rows)
        assert section.trade_count == sum(row.trade_count or 0 for row in rows)
        if section.largest_trade:
            assert section.largest_trade.volume == max(row.volume or 0 for row in rows)
    assert r.summary.total_volume == sum(s.volume for s in r.summary.sections)

    for g in gov:
        if g.trade_count:
            assert g.day_low_yield <= g.day_high_yield
        if g.closing_yield is None:
            assert g.volume is None  # unpriced securities never trade


def test_closing_yields_follow_end_of_day_pricing_not_the_last_trade():
    # Over many simulated days some traded bonds must close outside their
    # own day range, as in the sample (2023-GC-3: 13.63 vs 12.87-12.92).
    market, clock = _market(2026, 9, 28)
    outside = 0
    for day in range(1, 11):
        clock.set(2026, 10, day, 23, 59)
        r = market.report()
        outside += sum(
            1 for g in r.new_gog + r.ddep + r.old_gog
            if g.trade_count and not (g.day_low_yield <= g.closing_yield <= g.day_high_yield)
        )
    assert outside > 0


def test_the_next_session_opens_at_the_previous_close_and_resets_volume():
    market, clock = _market(2026, 9, 28)
    clock.set(2026, 9, 28, 23, 59)
    before = {g.symbol: g for g in market.report().ddep}
    clock.set(2026, 9, 29, 0, 0)
    after = market.report()
    assert after.report_date == date(2026, 9, 29)
    for g in after.ddep:
        assert g.opening_yield == before[g.symbol].closing_yield
        assert g.volume is None


# ---------------------------------------------------------------- weekly T-bill roll

def test_t_bills_roll_weekly():
    market, clock = _market(2026, 9, 28)
    clock.set(2026, 10, 5, 9, 0)  # the next Monday
    r = market.report()

    assert Counter(b.tenor for b in r.treasury_bill) == {"91-DAY BILL": 13, "182-DAY BILL": 26, "364-DAY BILL": 52}
    assert min(b.days_to_maturity for b in r.treasury_bill) == 7
    assert not any(b.maturity_date == date(2026, 10, 5) for b in r.treasury_bill)  # matured

    new = {b.tenor: b for b in r.treasury_bill if b.symbol.startswith("GHMK")}
    assert {t: b.maturity_date.isoformat() for t, b in new.items()} == {
        "91-DAY BILL": "2027-01-04", "182-DAY BILL": "2027-04-05", "364-DAY BILL": "2027-10-04",
    }
    assert all(b.opening_yield is None and b.closing_yield is not None for b in new.values())
    assert new["91-DAY BILL"].description == "GOG-BL-04/01/27-MOCK-0"


# ---------------------------------------------------------------- yield curve

def test_the_curve_is_the_reports_ghs_government_closes_by_tenor():
    market, clock = _market(2026, 9, 28)
    clock.set(2026, 9, 28, 15, 0)
    curve = market.curve()
    r = market.report()

    assert curve.date == date(2026, 9, 28) and curve.currency == "GHS"
    assert [p.days_to_maturity for p in curve.points] == sorted(p.days_to_maturity for p in curve.points)
    assert {p.segment for p in curve.points} == {"treasury_bill", "new_gog", "ddep", "old_gog"}

    # One point per bill maturity, carrying every bill that matures then.
    bill_points = [p for p in curve.points if p.segment == "treasury_bill"]
    assert len(bill_points) == len({b.maturity_date for b in r.treasury_bill})
    assert sum(len(p.instruments) for p in bill_points) == len(r.treasury_bill)
    closes = {b.symbol: b.closing_yield for b in r.treasury_bill}
    for p in bill_points:
        assert p.tenor_years == round(p.days_to_maturity / 365.25, 4)
        assert all(closes[i.symbol] == p.yield_ for i in p.instruments)

    # Bonds: quoted GHS ones only -- no GFSF, USD DDE or unpriced bonds.
    bonds = {p.instruments[0].symbol: p for p in curve.points if p.segment != "treasury_bill"}
    gov = [g for g in r.new_gog + r.ddep + r.old_gog
           if g.closing_yield is not None and g.currency == "GHS" and not g.tenor.startswith("GFSF")]
    assert set(bonds) == {g.symbol for g in gov}
    for g in gov:
        assert bonds[g.symbol].yield_ == g.closing_yield


def test_a_past_curve_is_that_sessions_close():
    market, clock = _market(2026, 9, 28)
    clock.set(2026, 9, 28, 23, 59)
    close = {p.maturity_date: p.yield_ for p in market.curve().points if p.segment == "treasury_bill"}
    clock.set(2026, 9, 30, 12, 0)
    market.report()  # trade on a couple more days

    past = market.curve(date(2026, 9, 28))
    assert past.date == date(2026, 9, 28)
    assert {p.maturity_date: p.yield_ for p in past.points if p.segment == "treasury_bill"} == close
    assert market.curve(date(2026, 9, 29)).date == date(2026, 9, 29)
    # Invented history goes back further; the 364-day bill issued on the
    # 28th wasn't there a month before.
    month = market.curve(date(2026, 8, 28))
    assert month.points and all(p.maturity_date != date(2027, 9, 27) for p in month.points)
    with pytest.raises(ValueError):
        market.curve(date(2026, 10, 1))


def test_curve_api(client):
    body = client.get("/curve").json()
    assert body["currency"] == "GHS" and body["points"]
    p = body["points"][0]
    assert {"tenor_years", "yield", "segment", "maturity_date", "instruments"} <= set(p)
    assert client.get("/curve", params={"date": body["date"]}).json()["date"] == body["date"]
    assert client.get("/curve", params={"date": "2999-01-01"}).status_code == 400
    assert client.get("/curve", params={"date": "not-a-date"}).status_code == 422
    assert client.get("/yield-curve").status_code == 200


# ---------------------------------------------------------------- API

def test_fixed_income_api(client):
    report = client.get("/fixed-income/report")
    assert report.status_code == 200
    body = report.json()
    assert set(REPORT_SECTIONS) <= set(body)
    assert len(body["treasury_bill"]) == 91
    assert "yield" in body["sell_buy_back"][0]  # alias, not "yield_"

    summary = client.get("/fixed-income/summary").json()
    assert [s["section"] for s in summary["sections"]] == list(REPORT_SECTIONS)

    ddep = client.get("/fixed-income/ddep").json()
    assert len(ddep) == 29
    GovernmentBondQuote(**ddep[0])
    assert client.get("/fixed-income/equities").status_code == 422
