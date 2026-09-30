import asyncio
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from app import config
from app.aggregation.market_aggregator import MarketAggregator
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.models.candle import Candle, resample
from app.models.fixed_income import FixedIncomeTick, RepoTick
from app.models.market_data import MarketData
from app.models.tick import TICK_ADAPTER
from app.processors.market_processor import MarketProcessor
from app.queue.market_buffer import MarketDataBuffer
from app.validation.market_validator import validate_candle, validate_tick

GC3 = "GHGGOG069931"     # 2023-GC-3, quoted by yield
LETSHEGO = "GHCLGH075744"  # a corporate, quoted by price only


def _now():
    return datetime.now(timezone.utc)


def _gov(**overrides) -> FixedIncomeTick:
    base = dict(
        symbol=GC3, name="GOG-BD-13/02/29-A6187-1839-8.65", segment="ddep", currency="GHS",
        maturity_date=date(2029, 2, 13),
        bid_yield=13.70, ask_yield=13.60, bid_price=89.90, ask_price=90.10,
        opening_yield=13.60, closing_yield=13.63, day_low_yield=12.87, day_high_yield=12.92,
        closing_price=90.1021, volume=0, trade_count=0, timestamp=_now(),
    )
    base.update(overrides)
    return FixedIncomeTick(**base)


def _corporate(**overrides) -> FixedIncomeTick:
    base = dict(
        symbol=LETSHEGO, name="LGH-BD-07/10/27-C0935-22.5", segment="corporate", currency="GHS",
        maturity_date=date(2027, 10, 7), opening_price=98.4591, closing_price=98.4591,
        timestamp=_now(),
    )
    base.update(overrides)
    return FixedIncomeTick(**base)


def _repo(**overrides) -> RepoTick:
    base = dict(
        symbol=GC3, name="GOG-BD-13/02/29-A6187-1839-8.65", segment="ddep", currency="GHS",
        maturity_date=date(2029, 2, 13), bond_yield=14.1, bond_price=88.5,
        volume=20_000_000, trade_count=1, timestamp=_now(), last_trade_at=_now(),
    )
    base.update(overrides)
    return RepoTick(**base)


# ---------------------------------------------------------------- the union

def test_the_union_parses_each_kind_from_its_tick_type(make_tick):
    for tick in (make_tick(), _gov(), _repo()):
        parsed = TICK_ADAPTER.validate_python(tick.model_dump(mode="json"))
        assert type(parsed) is type(tick) and parsed == tick
    assert make_tick().tick_type == "equity"


def test_prices_and_yields_are_optional():
    # Corporates are quoted by price only; an unquoted bond has neither.
    corp = _corporate()
    assert corp.closing_yield is None and corp.bid_yield is None
    blank = FixedIncomeTick(
        symbol="GHGGOG072851", name="GOG-BD-24/11/37-A6392-1879-9.85", segment="ddep",
        currency="GHS", maturity_date=date(2037, 11, 24), timestamp=_now(),
    )
    assert blank.closing_price is None and blank.volume == 0
    assert validate_tick(corp) and validate_tick(blank)


# ---------------------------------------------------------------- validator

def test_a_well_formed_bond_tick_passes_even_closing_outside_its_day_range():
    # 13.63 against 12.87-12.92, as in the sample (quirk 7): not an error.
    assert validate_tick(_gov()).errors == []


@pytest.mark.parametrize("field", ["bid_price", "ask_price", "closing_price", "day_low_price"])
@pytest.mark.parametrize("value", [0, -1.5])
def test_prices_must_be_positive(field, value):
    overrides = {field: value}
    if field == "ask_price":
        overrides["bid_price"] = None  # keep the bid/ask rule out of it
    result = validate_tick(_gov(**overrides))
    assert not result and any(field in e and "not positive" in e for e in result.errors)


def test_yields_must_be_within_the_configured_bounds(monkeypatch):
    assert not validate_tick(_gov(closing_yield=-0.5))
    assert not validate_tick(_gov(day_high_yield=150.0))
    # 58.59% really happened on the sample day (an Old GoG 15-year bond).
    assert validate_tick(_gov(closing_yield=58.59))

    assert not validate_tick(_gov(), yield_bounds=(0, 13))  # 13.60+ everywhere
    monkeypatch.setattr(config, "FI_YIELD_MAX", 13.0)
    result = validate_tick(_gov())
    assert not result and any("outside [0.0, 13.0]" in e for e in result.errors)


def test_a_matured_security_is_rejected():
    today = _now().date()
    assert not validate_tick(_gov(maturity_date=today))
    assert not validate_tick(_gov(maturity_date=today - timedelta(days=1)))
    assert validate_tick(_gov(maturity_date=today + timedelta(days=1)))


def test_bid_yield_must_not_be_below_ask_yield():
    result = validate_tick(_gov(bid_yield=13.5, ask_yield=13.6))
    assert not result and any("bid_yield" in e for e in result.errors)
    assert validate_tick(_gov(bid_yield=13.6, ask_yield=13.6))  # locked is fine
    assert validate_tick(_gov(bid_yield=13.6, ask_yield=None))   # one-sided is fine


def test_a_crossed_price_book_is_rejected():
    result = validate_tick(_gov(bid_price=90.2, ask_price=90.1))
    assert not result and any("bid_price" in e for e in result.errors)


def test_fixed_income_ticks_get_the_equity_clock_rules():
    assert not validate_tick(_gov(timestamp=_now() - timedelta(minutes=5)))
    result = validate_tick(_gov(volume=50_000, trade_count=1))
    assert not result and any("no last_trade_at" in e for e in result.errors)


def test_repo_ticks_are_validated_too():
    assert validate_tick(_repo())
    assert validate_tick(_repo(bond_yield=None, repo_rate=None))  # nothing to check is fine
    assert not validate_tick(_repo(bond_price=0))
    assert not validate_tick(_repo(repo_rate=-1.0))
    assert not validate_tick(_repo(maturity_date=date(2020, 1, 1)))


def test_a_price_in_the_repo_yield_field_is_rejected_even_within_bounds():
    # Quirk 12: USD-DDE-FEA-28's sell/buy-back "yield" is its price, 86.80,
    # which the default 0-100 yield bounds would let through.
    result = validate_tick(_repo(bond_yield=86.8, bond_price=86.8))
    assert not result and any("price in the yield field" in e for e in result.errors)


def test_the_mock_leaves_a_price_in_the_yield_column_unmapped():
    from app.connectors.fixed_income_mock import repo_bond_yield
    from app.models.fixed_income import SellBuyBackQuote

    row = dict(symbol="GHGGOG071713", description="GOG-BD-04/09/28-A6307-1866-3.25", segment="ddep",
               tenor="USD-DDE-FEA-28", maturity_date=date(2028, 9, 4), days_to_maturity=707,
               volume=1_000_000, trade_count=1)
    assert repo_bond_yield(SellBuyBackQuote(**row, yield_=86.8, weighted_average_price=86.8)) is None
    assert repo_bond_yield(SellBuyBackQuote(**row, yield_=5.7689, weighted_average_price=86.8)) == 5.7689


# ---------------------------------------------------------------- candles

def _yield_candle(start, o, h, l, c, yields):
    yo, yh, yl, yc = yields
    return Candle(
        symbol=GC3, interval="5m", window_start=start, window_end=start + timedelta(minutes=5),
        open=o, high=h, low=l, close=c, volume=0,
        yield_open=yo, yield_high=yh, yield_low=yl, yield_close=yc,
    )


def test_validate_candle_checks_the_yield_range():
    t0 = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    assert validate_candle(_yield_candle(t0, 90, 91, 89, 90.5, (13.6, 13.8, 13.4, 13.5)))
    assert not validate_candle(_yield_candle(t0, 90, 91, 89, 90.5, (13.6, 13.4, 13.8, 13.5)))
    assert not validate_candle(_yield_candle(t0, 90, 91, 89, 90.5, (13.9, 13.8, 13.4, 13.5)))
    assert not validate_candle(_yield_candle(t0, 90, 91, 89, 90.5, (13.6, None, 13.4, 13.5)))


def test_resample_carries_yield():
    t0 = datetime(2026, 9, 28, 10, 0, tzinfo=timezone.utc)
    fine = [
        _yield_candle(t0, 90, 91, 89, 90.5, (13.6, 13.8, 13.4, 13.5)),
        _yield_candle(t0 + timedelta(minutes=5), 90.5, 92, 90, 91, (13.5, 13.5, 13.1, 13.2)),
    ]
    (c,) = resample(fine, "15m")
    assert (c.open, c.high, c.low, c.close) == (90, 92, 89, 91)
    assert (c.yield_open, c.yield_high, c.yield_low, c.yield_close) == (13.6, 13.8, 13.1, 13.2)


async def _feed(ticks):
    for tick in ticks:
        yield tick


async def _aggregate(ticks, db_path) -> MarketAggregator:
    buffer = MarketDataBuffer(_feed(ticks), maxsize=100)
    aggregator = MarketAggregator(buffer.subscribe("aggregator"), db_path=db_path, flush_interval_seconds=3600)
    await buffer.start()
    await aggregator.start()
    try:
        for _ in range(100):
            if aggregator._windows or aggregator._repos:
                await asyncio.sleep(0.05)  # let the rest of the feed through
                break
            await asyncio.sleep(0.02)
        await aggregator._flush()
    finally:
        await aggregator.stop(final_flush=False)
        await buffer.stop()
    return aggregator


async def test_aggregator_charts_clean_price_with_yield_and_keeps_repos_out(make_tick, tmp_path):
    t0 = _now()
    traded = t0 - timedelta(seconds=1)
    ticks = [
        make_tick(timestamp=t0),
        _gov(timestamp=t0, closing_price=90.10, closing_yield=13.63),
        _repo(timestamp=t0, bond_price=50.0, bond_yield=40.0, repo_rate=18.0),  # must not reach the candle
        _gov(timestamp=t0, closing_price=90.60, closing_yield=13.40, volume=50_000, trade_count=1,
             last_trade_at=traded),
        _gov(timestamp=t0, closing_price=89.90, closing_yield=13.70, volume=80_000, trade_count=2,
             last_trade_at=traded),
        _corporate(timestamp=t0),
        FixedIncomeTick(  # unquoted: no candle at all
            symbol="GHGGOG072851", name="GFSF-5-14YR", segment="ddep", currency="GHS",
            maturity_date=date(2037, 11, 24), timestamp=t0,
        ),
    ]
    aggregator = await _aggregate(ticks, tmp_path / "candles.db")

    (bond,) = await aggregator.get_candles(GC3, "15m")
    assert (bond.open, bond.high, bond.low, bond.close) == (90.10, 90.60, 89.90, 89.90)
    assert (bond.yield_open, bond.yield_high, bond.yield_low, bond.yield_close) == (13.63, 13.70, 13.40, 13.70)
    assert bond.tick_count == 3 and bond.volume == 80_000
    assert validate_candle(bond)

    (corp,) = await aggregator.get_candles(LETSHEGO, "15m")
    assert corp.close == 98.4591 and corp.yield_close is None
    (equity,) = await aggregator.get_candles("MTNGH", "15m")
    assert equity.yield_close is None
    assert await aggregator.get_candles("GHGGOG072851", "15m") == []

    # Repos: their own table, never market_candles.
    (repo,) = await aggregator.get_repo_trades(GC3)
    assert (repo["bond_price"], repo["bond_yield"], repo["repo_rate"], repo["volume"]) == (50.0, 40.0, 18.0, 20_000_000)
    conn = sqlite3.connect(tmp_path / "candles.db")
    try:
        stored = conn.execute(
            "SELECT high, low, yield_high, yield_low FROM market_candles WHERE symbol = ?", (GC3,)
        ).fetchall()
        repo_rows = conn.execute("SELECT symbol, volume FROM repo_trades").fetchall()
    finally:
        conn.close()
    assert all(r == (90.60, 89.90, 13.70, 13.40) for r in stored)
    assert repo_rows == [(GC3, 20_000_000)]


def test_an_existing_candle_table_gains_the_yield_columns(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE market_candles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, interval TEXT NOT NULL,
            window_start TEXT NOT NULL, window_end TEXT NOT NULL, open REAL NOT NULL,
            high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL, volume INTEGER NOT NULL,
            tick_count INTEGER NOT NULL, created_at TEXT NOT NULL,
            UNIQUE (symbol, interval, window_start))"""
    )
    conn.execute(
        "INSERT INTO market_candles VALUES (NULL, 'MTNGH', '5m', '2026-09-28T10:00:00+00:00', "
        "'2026-09-28T10:05:00+00:00', 6.5, 6.6, 6.4, 6.55, 100, 3, 'x')"
    )
    conn.commit()
    conn.close()

    aggregator = MarketAggregator(_feed([]), db_path=db)
    (c,) = asyncio.run(aggregator.get_candles("MTNGH", "5m"))
    assert c.close == 6.55 and c.yield_close is None


# ---------------------------------------------------------------- processor and buffer

async def test_a_repo_tick_never_replaces_the_bonds_quote():
    processor = MarketProcessor()
    quote, repo = _gov(), _repo()
    await processor.process(quote)
    await processor.process(repo)
    assert processor.get_latest(GC3) is quote
    assert processor.get_latest_repo(GC3) is repo


async def test_conflated_subscribers_keep_a_bonds_quote_and_repo_apart():
    quote, repo = _gov(), _repo()
    buffer = MarketDataBuffer(_feed([quote, repo]), maxsize=10)
    feed = buffer.subscribe_latest("latest")
    await buffer.start()
    try:
        seen = [await asyncio.wait_for(feed.__anext__(), 1) for _ in range(2)]
    finally:
        await buffer.stop()
    assert {t.tick_type for t in seen} == {"fixed_income", "repo"}


# ---------------------------------------------------------------- the mock as a tick source

class _Clock:
    def __init__(self, *args):
        self.now = datetime(*args, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def test_the_mock_market_yields_valid_ticks_for_every_security():
    clock = _Clock(2026, 9, 28)
    market = MockFixedIncomeMarket(clock=clock)
    market.start()

    ticks = market.ticks()
    fi = [t for t in ticks if isinstance(t, FixedIncomeTick)]
    assert len(fi) == 2 + 29 + 17 + 91 + 22
    assert not any(isinstance(t, RepoTick) for t in ticks)  # nothing traded at midnight
    assert {t.currency for t in fi if t.name.endswith(("-2.75", "-3.25"))} == {"USD"}
    assert all(t.closing_yield is None for t in fi if t.segment == "corporate")

    clock.now = datetime(2026, 9, 28, 16, 0, tzinfo=timezone.utc)
    ticks = market.ticks()
    repos = [t for t in ticks if isinstance(t, RepoTick)]
    assert repos and all(t.segment in ("new_gog", "ddep") and t.volume > 0 for t in repos)
    traded = [t for t in ticks if isinstance(t, FixedIncomeTick) and t.volume]
    assert traded and all(t.last_trade_at is not None and t.last_trade_at <= t.timestamp for t in traded)

    rejected = [(t.tick_type, t.symbol, r.errors) for t in ticks if not (r := validate_tick(t, now=clock.now))]
    assert rejected == []
    assert not any(isinstance(t, MarketData) for t in ticks)
