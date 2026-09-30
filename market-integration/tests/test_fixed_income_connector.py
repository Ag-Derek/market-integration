"""The mock fixed income connector (#35): a drifting curve behind every
quote, prices from the bond math, GFIM sessions, invented history, per-
instrument tick rates and burst mode, and the composite feed that puts
it beside the equities."""

import asyncio
import random
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import config
from app.bond_math import Bond, bill_price, clean_price, supports
from app.connectors.base_connector import BaseMarketConnector
from app.connectors.composite_connector import CompositeConnector
from app.connectors.fixed_income_connector import MockFixedIncomeConnector
from app.connectors.fixed_income_mock import HALF_SPREAD, MockFixedIncomeMarket
from app.instruments import INSTRUMENTS
from app.models.candle import bucket_start
from app.models.fixed_income import FixedIncomeTick, RepoTick
from app.session.calendar import MarketCalendar
from app.validation.market_validator import validate_candle, validate_tick

GC3 = "GHGGOG069931"         # 2023-GC-3
BILL = "GHGGOGI01883"        # 364-day, matures 21-Jun-2027
BILL_91 = "GHGGOGI02303"     # 91-day issued 28-Sep-2026, matures 28-Dec-2026
BILL_364 = "GHGGOGI01032"    # 364-day maturing the same day
LETSHEGO = "GHCLGH075744"    # a corporate, quoted by price only
UNIVERSE = 2 + 29 + 17 + 91 + 22


class _Clock:
    def __init__(self, *args):
        self.now = datetime(*args, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def set(self, *args):
        self.now = datetime(*args, tzinfo=timezone.utc)

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


def _gfim_calendar() -> MarketCalendar:
    return MarketCalendar(tz=timezone.utc, timezone_name="UTC", trading_days={0, 1, 2, 3, 4},
                          pre_open=time(9), open=time(9), close=time(16), holidays={})


def _market(*when, calendar=None, history=False):
    clock = _Clock(*when)
    market = MockFixedIncomeMarket(clock=clock, calendar=calendar, history=history)
    market.start()
    return market, clock


def _price_function(tick: FixedIncomeTick, session: date):
    if tick.segment == "treasury_bill":
        return lambda y: bill_price(session, tick.maturity_date, y)
    inst = INSTRUMENTS[tick.symbol]
    if not supports(inst):
        return None  # GFSF and USD DDE: priced with a calibrated offset
    bond = Bond.from_instrument(inst)
    return lambda y: clean_price(bond, session, y)


# ---------------------------------------------------------------- prices and quotes

def test_every_price_is_derived_from_its_yield_with_the_bond_math():
    random.seed(35)
    market, clock = _market(2026, 9, 28)
    clock.set(2026, 9, 28, 15)
    checked = 0
    for t in market.ticks():
        if not isinstance(t, FixedIncomeTick) or t.closing_yield is None:
            continue
        price = _price_function(t, date(2026, 9, 28))
        if price is None:
            continue
        # Both sides are rounded to 4 dp, which is ~1e-3 of price on a long bond.
        for y, p in ((t.closing_yield, t.closing_price), (t.bid_yield, t.bid_price), (t.ask_yield, t.ask_price)):
            assert p == pytest.approx(price(y), abs=1e-3), (t.symbol, y, p)
        checked += 1
    assert checked > 100


def test_two_way_quotes_straddle_the_fair_yield_and_drift_between_trades():
    random.seed(1)
    market, clock = _market(2026, 9, 28, 10)
    before = {t.symbol: t for t in market.ticks() if isinstance(t, FixedIncomeTick)}
    clock.set(2026, 10, 2, 10)
    after = {t.symbol: t for t in market.ticks() if isinstance(t, FixedIncomeTick)}

    gc3 = after[GC3]
    assert gc3.bid_yield - gc3.ask_yield == pytest.approx(2 * HALF_SPREAD["ddep"], abs=2e-4)
    assert gc3.bid_price < gc3.ask_price
    quoted = [s for s, t in after.items() if t.bid_yield is not None]
    assert len(quoted) > 100
    moved = [s for s in quoted if s in before and after[s].bid_yield != before[s].bid_yield]
    assert len(moved) == len([s for s in quoted if s in before])
    assert all(after[s].bid_yield is None for s in after if after[s].segment == "corporate")


def test_the_curve_is_fitted_to_the_sample_and_drifts_plausibly():
    random.seed(2)
    market, clock = _market(2026, 9, 28)
    curve = market._curves["GHS"]
    # The sample: 91-day bills ~4.7%, 364-day ~9.8%, long DDEP bonds ~15%.
    assert 3 < curve.yield_at(0.25) < 7
    assert 13 < curve.yield_at(10) < 17
    start = curve.factors()

    clock.set(2027, 9, 28)
    ticks = market.ticks()
    assert curve.factors() != start
    assert all(validate_tick(t, now=clock()) for t in ticks)
    assert all(0 < t.closing_yield < 60 for t in ticks
               if isinstance(t, FixedIncomeTick) and t.closing_yield is not None)


# ---------------------------------------------------------------- sessions

def test_with_a_calendar_it_trades_only_in_gfim_hours():
    random.seed(3)
    market, clock = _market(2026, 10, 2, 8, 0, calendar=_gfim_calendar())  # Friday, before the open
    assert market.report().report_date == date(2026, 10, 1)  # Thursday's session so far
    assert not market.is_open()

    clock.set(2026, 10, 2, 9, 0)
    assert market.report().report_date == date(2026, 10, 2) and market.is_open()

    clock.set(2026, 10, 5, 8, 59)  # Monday, before the open
    r = market.report()
    assert r.report_date == date(2026, 10, 2)
    trades = [t for t in market._last_trade.values()]
    assert trades and all(time(9) <= t.time() < time(16) and t.weekday() < 5 for t in trades)

    clock.set(2026, 10, 5, 9, 0)
    r = market.report()
    assert r.report_date == date(2026, 10, 5) and r.summary.total_volume == 0


# ---------------------------------------------------------------- history

@pytest.fixture(scope="module")
def history_market():
    random.seed(7)
    return _market(2026, 9, 30, 12, history=True)


@pytest.mark.parametrize("symbol", [GC3, BILL, LETSHEGO])
def test_history_is_valid_on_every_interval_and_ends_now(history_market, symbol):
    market, clock = history_market
    for interval in ("5m", "15m", "1h", "1d", "1w"):
        candles = market.history(symbol, interval)
        assert candles, interval
        assert all(validate_candle(c) for c in candles)
        assert candles[-1].window_start == bucket_start(clock(), interval)
    assert sum(c.volume for c in market.history(symbol, "1d")) > 0


def test_history_lands_on_the_live_close(history_market):
    market, _ = history_market
    gc3, corp = market.tick(GC3), market.tick(LETSHEGO)
    assert market.history(GC3, "1d")[-1].yield_close == gc3.closing_yield
    assert market.history(LETSHEGO, "1d")[-1].close == corp.closing_price
    assert market.history(LETSHEGO, "1d")[-1].yield_close is None  # price-only


def test_history_prices_are_derived_from_its_yields(history_market):
    market, _ = history_market
    bond = Bond.from_instrument(INSTRUMENTS[GC3])
    for c in market.history(GC3, "1d")[-400:]:
        day = c.window_start.date()
        assert c.close == pytest.approx(clean_price(bond, day, c.yield_close), abs=1e-3)
        assert c.high == pytest.approx(clean_price(bond, day, c.yield_low), abs=1e-3)  # high price = low yield


def test_history_starts_at_each_securitys_issue(history_market):
    market, _ = history_market
    assert market.history(GC3, "1d")[0].window_start.date() == date(2023, 2, 21)  # DDEP settlement
    assert market.history(BILL_91, "1d")[0].window_start.date() == date(2026, 9, 28)
    assert market.history(BILL_364, "1d")[0].window_start.date() == date(2025, 12, 29)
    # Same maturity, same closes, where both exist.
    short = {c.window_start: c.yield_close for c in market.history(BILL_91, "1d")}
    long_ = {c.window_start: c.yield_close for c in market.history(BILL_364, "1d")}
    assert all(long_[t] == y for t, y in short.items())


def test_history_follows_a_plausible_curve(history_market):
    market, _ = history_market
    yields = [c.yield_close for c in market.history(GC3, "1d")]
    assert 3 < min(yields) and max(yields) < 30
    assert len(set(yields)) > 50  # it moves


# ---------------------------------------------------------------- connector

async def _connector(*when, calendar=None, **kwargs):
    random.seed(4)
    clock = _Clock(*when)
    market = MockFixedIncomeMarket(clock=clock, calendar=calendar, history=False)
    connector = MockFixedIncomeConnector(market, clock=clock, **kwargs)
    await connector.connect()
    return connector, clock


async def test_snapshot_then_quotes_at_each_instruments_own_rate():
    quiet = {"new_gog": 0, "ddep": 0, "old_gog": 0}
    connector, clock = await _connector(2026, 9, 28, 10, quote_intervals={**quiet, "treasury_bill": 60, BILL: 1})
    snapshot = connector.snapshot(clock())
    assert len(snapshot) == UNIVERSE and {t.symbol for t in snapshot} == set(connector.symbols)

    counts = Counter()
    for _ in range(59):
        clock.advance(seconds=1)
        counts.update(t.symbol for t in connector.poll(clock()) if isinstance(t, FixedIncomeTick))
    assert counts[BILL] == 59  # its own 1s rate
    bills = [s for s in connector.symbols if connector.market.segment(s) == "treasury_bill"]
    assert max(counts[s] for s in bills if s != BILL) <= 3  # only when traded

    clock.advance(seconds=1)  # the 60s segment rate comes due for every bill
    # that hasn't ticked since (a trade's tick carries a fresh quote too)
    quiet_bills = {s for s in bills if s != BILL and counts[s] == 0}
    assert len(quiet_bills) > 80
    assert quiet_bills <= {t.symbol for t in connector.poll(clock())}


async def test_burst_mode_quotes_everything_even_when_the_market_is_closed():
    connector, clock = await _connector(2026, 10, 3, 12, calendar=_gfim_calendar())  # Saturday
    connector.snapshot(clock())
    clock.advance(seconds=1)
    assert connector.poll(clock()) == []

    connector.burst(rate=100, seconds=2)
    clock.advance(seconds=1)
    ticks = connector.poll(clock())
    assert len(ticks) == UNIVERSE
    assert all(validate_tick(t, now=clock()) for t in ticks)

    clock.advance(seconds=2)
    assert connector.poll(clock()) == [] and not connector.bursting()


async def test_the_stream_sends_the_snapshot_then_keeps_going():
    connector, clock = await _connector(2026, 9, 28, 10, burst_rate=1000)
    got = []
    async for tick in connector.stream():
        got.append(tick)
        clock.advance(milliseconds=1)
        if len(got) == 2 * UNIVERSE:
            break
    await connector.disconnect()
    assert Counter(t.symbol for t in got[:UNIVERSE]) == Counter(connector.symbols)
    assert all(isinstance(t, (FixedIncomeTick, RepoTick)) for t in got)
    assert {t.exchange_label for t in got if isinstance(t, FixedIncomeTick)} == {
        "GFIM - Simulated Quote - GHS", "GFIM - Simulated Quote - USD",
    }


async def test_newly_issued_bills_join_the_symbols():
    connector, clock = await _connector(2026, 9, 28, 10)
    clock.set(2026, 10, 5, 10)  # next Monday's issue
    connector.poll(clock())
    assert "GHMKA2701040" in connector.symbols  # 91-day maturing 4-Jan-2027
    assert connector.market.asset_class("GHMKA2701040") == "bill"


def test_quote_intervals_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("FI_QUOTE_INTERVALS", "Treasury_Bill=2, ghggog069931=0.5,corporate=0")
    assert config._quote_intervals_from_env() == {"treasury_bill": 2.0, GC3: 0.5, "corporate": 0.0}
    monkeypatch.setenv("FI_QUOTE_INTERVALS", "ddep")
    with pytest.raises(ValueError):
        config._quote_intervals_from_env()


# ---------------------------------------------------------------- composite

class _Fake(BaseMarketConnector):
    def __init__(self, symbols, ticks, fail=None, heartbeat=None):
        super().__init__(list(symbols))
        self._ticks, self._fail, self._heartbeat = ticks, fail, heartbeat
        self.history_calls = []

    async def connect(self):
        self.running = True
        self.last_heartbeat = self._heartbeat

    async def disconnect(self):
        self.running = False

    async def stream(self):
        for tick in self._ticks:
            yield tick
            await asyncio.sleep(0)
        if self._fail:
            raise self._fail

    async def fetch_history(self, symbol, interval, start=None):
        self.history_calls.append(symbol)
        return []


async def test_composite_merges_streams_and_routes_history(make_tick):
    early = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    a = _Fake(["MTNGH"], [make_tick("MTNGH")] * 3, heartbeat=early + timedelta(seconds=5))
    b = _Fake(["GCB"], [make_tick("GCB")] * 2, heartbeat=early)
    composite = CompositeConnector([a, b])
    assert not composite.running and composite.last_heartbeat is None
    await composite.connect()

    assert composite.running and composite.symbols == ["MTNGH", "GCB"]
    assert composite.last_heartbeat == early  # the stalest child
    assert Counter([t.symbol async for t in composite.stream()]) == {"MTNGH": 3, "GCB": 2}

    b.symbols.append("CAL")  # read live
    assert composite.symbols == ["MTNGH", "GCB", "CAL"]
    await composite.fetch_history("CAL", "1d")
    assert b.history_calls == ["CAL"] and a.history_calls == []
    await composite.disconnect()
    assert not composite.running


async def test_composite_stream_fails_when_a_child_does(make_tick):
    composite = CompositeConnector([
        _Fake(["MTNGH"], [make_tick("MTNGH")], fail=RuntimeError("feed down")),
        _Fake(["GCB"], [make_tick("GCB")] * 100),
    ])
    await composite.connect()
    with pytest.raises(RuntimeError, match="feed down"):
        async for _ in composite.stream():
            pass


# ---------------------------------------------------------------- the app

def test_the_app_streams_fixed_income_labelled_as_simulated(client):
    tick = client.get(f"/market/{GC3}").json()
    assert tick["tick_type"] == "fixed_income" and "Simulated" in tick["exchange_label"]
    assert tick["bid_yield"] > tick["ask_yield"]
    assert "Simulated" in client.get("/fixed-income/report").json()["exchange_label"]

    candles = client.get("/candles", params={"symbol": GC3, "range": "1Y"}).json()["candles"]
    assert len(candles) > 200  # backfilled history
    assert all(c["yield"] is not None for c in candles)
    assert client.get(f"/stock/{GC3}").status_code == 404  # bonds get their own page (#39)


# ---------------------------------------------------------------- the Fixed Income tab (#37)

def test_the_fixed_income_tab_and_bond_pages_are_served(client):
    page = client.get("/fixed-income")
    assert page.status_code == 200 and "Fixed Income" in page.text
    assert 'href="/fixed-income"' in client.get("/ticker").text  # the Equities | Fixed Income tabs
    assert client.get(f"/bond/{GC3}").status_code == 200
    assert client.get("/bond/MTNGH").status_code == 404  # equities have /stock
    assert client.get("/bond/NOPE").status_code == 404


def test_the_status_carries_the_gfim_session_for_the_fixed_income_badge(client):
    status = client.get("/market/status").json()
    assert status["exchange"] == "GSE"
    gfim = status["fixed_income"]
    assert gfim["exchange"] == "GFIM"
    assert gfim["session"]["open"] == "09:00" and gfim["session"]["close"] == "16:00"
    assert gfim["badge"] in ("live", "delayed", "closed", "disconnected")


def test_every_security_has_a_quote_after_the_opening_snapshot_burst(client):
    # Equities and fixed income each send a snapshot of every security at
    # startup, together more than a subscriber queue holds; none may be
    # dropped (the first equity's used to be).
    import time as _time
    from app.main import connector, processor
    deadline = _time.time() + 10
    while _time.time() < deadline and any(processor.get_latest(s) is None for s in connector.symbols):
        _time.sleep(0.1)
    assert [s for s in connector.symbols if processor.get_latest(s) is None] == []
