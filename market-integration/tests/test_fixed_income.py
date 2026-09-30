from collections import Counter
from datetime import date, datetime, timezone

import pytest

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

    # Closing values are the sample's.
    gc3 = _row(r.ddep, tenor="2023-GC-3")
    assert (gc3.closing_yield, gc3.closing_price) == (13.63, 90.1021)
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
