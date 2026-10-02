import asyncio
import csv
import io
import logging
import sqlite3
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)

from app import config, metrics
from app.aggregation.market_aggregator import MarketAggregator
from app.aggregation.ranges import RANGES
from app.bond_math import settlement_date
from app.bond_math.calculator import calculate, quoted_yield, security_for
from app.company import COMPANIES
from app.company.figures import INDEX_NAME, calculated_figures, with_market_cap
from app.company.index import BASE_LEVEL, simulated_gse_ci
from app.company.performance import Day, days
from app.company.store import CompanyStore
from app.connectors.composite_connector import CompositeConnector
from app.connectors.fixed_income_connector import MockFixedIncomeConnector
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.connectors.load_connector import LoadConnector
from app.connectors.market_connector import MockMarketConnector
from app.connectors.supervisor import SupervisedConnector
from app.gateways.websocket_gateway import WebSocketGateway
from app.instruments import INSTRUMENTS
from app.instruments.search import InstrumentSearch
from app.instruments.store import InstrumentStore
from app.models.fixed_income import (
    FixedIncomeReport,
    FixedIncomeSummary,
    FixedIncomeTick,
    GovernmentYieldCurve, ReportSection,
)
from app.models.company import Company
from app.models.instrument import AssetClass, Instrument
from app.models.market_data import MarketData
from app.processors.market_processor import MarketProcessor
from app.processors.movers import MoverType, change_percent, rank_movers
from app.queue.market_buffer import MarketDataBuffer
from app.session.calendar import load_calendar
from app.session.status import market_status
from app.validation.market_validator import ValidatingStream

logger = logging.getLogger(__name__)

app = FastAPI(title="Market Data Integration Service")

STATIC_DIR = Path(__file__).parent / "static"

# The exchange's session hours and holidays. The mock only trades while
# this says the market is open.
calendar = load_calendar(config.MARKET_CALENDAR_PATH, override=config.MARKET_SESSION_OVERRIDE)

# GFIM trades longer hours than the equity market, on the same days.
fixed_income_calendar = calendar.with_hours(
    open=time.fromisoformat(config.FI_SESSION_OPEN),
    close=time.fromisoformat(config.FI_SESSION_CLOSE),
    exchange="GFIM",
    hours_source="GFIM Rules 2022, Rule 12: trading 09:00-16:00 GMT",
)

if config.FEED_MODE == "load":
    # Synthetic equities at a fixed rate, for load tests and benchmarks.
    equities = LoadConnector(
        instruments=config.LOAD_INSTRUMENTS,
        ticks_per_second=config.LOAD_TICKS_PER_SECOND,
        ticks_per_instrument=config.LOAD_TICKS_PER_INSTRUMENT,
        burst_multiplier=config.LOAD_BURST_MULTIPLIER,
        burst_seconds=config.LOAD_BURST_SECONDS,
        burst_after=config.LOAD_BURST_AFTER_SECONDS,
        burst_every=config.LOAD_BURST_EVERY_SECONDS,
    )
else:
    equities = MockMarketConnector(
        symbols=config.SYMBOLS,
        interval_seconds=config.MOCK_INTERVAL_SECONDS,
        calendar=calendar,
    )

# Fixed income (GFIM): the mock market behind both the tick stream and
# the GFIM-style daily report endpoints.
fixed_income = MockFixedIncomeMarket(calendar=fixed_income_calendar)
fixed_income_connector = MockFixedIncomeConnector(
    fixed_income,
    interval_seconds=config.FI_MOCK_INTERVAL_SECONDS,
    quote_intervals=config.FI_QUOTE_INTERVALS,
    burst_rate=config.FI_BURST_RATE,
)

# Each feed reconnects on its own if it drops (with backoff; see
# connectors/supervisor.py), so an equities outage doesn't take the
# fixed-income feed down with it, nor the service.
_status_tasks: set[asyncio.Task] = set()


def _on_feed_change(feed: SupervisedConnector) -> None:
    """Tell WebSocket clients at once when a feed drops or recovers,
    rather than at the next periodic status message."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(gateway.broadcast_status(current_status()))
    _status_tasks.add(task)
    task.add_done_callback(_status_tasks.discard)


def _supervised(feed, name: str) -> SupervisedConnector:
    return SupervisedConnector(
        feed,
        name=name,
        initial_delay=config.RECONNECT_INITIAL_DELAY_SECONDS,
        max_delay=config.RECONNECT_MAX_DELAY_SECONDS,
        jitter=config.RECONNECT_JITTER,
        on_change=_on_feed_change,
    )


equities_feed = _supervised(equities, "equities")
fixed_income_feed = _supervised(fixed_income_connector, "fixed_income")
feeds = (equities_feed, fixed_income_feed)

# One feed for the pipeline: equities and fixed income ticks together.
connector = CompositeConnector(list(feeds))


def asset_class(symbol: str) -> str:
    if symbol in equities.symbols:
        return "equity"
    return fixed_income.asset_class(symbol)


def _reconnecting(*candidates: SupervisedConnector) -> Optional[dict]:
    """The first of these feeds that is retrying, as status reports it."""
    feed = next((f for f in candidates if f.reconnecting), None)
    return None if feed is None else {"feed": feed.name, **feed.describe()}


def current_status() -> dict:
    """Session state plus feed freshness: what the UI's badge shows. The
    top level is the equity market's; `fixed_income` is the same for
    GFIM, whose session is longer, for the Fixed Income tab's badge.
    While a feed is being reconnected, feed.state is "reconnecting" and
    feed.reconnect says which feed, which attempt and when the next one
    is."""
    now = datetime.now(timezone.utc)
    stale_after = timedelta(seconds=config.FEED_STALE_SECONDS)
    status = market_status(
        calendar,
        running=connector.running,
        last_heartbeat=connector.last_heartbeat,
        now=now,
        stale_after=stale_after,
        reconnect=_reconnecting(*feeds),
    )
    status["fixed_income"] = market_status(
        fixed_income_calendar,
        running=fixed_income_feed.running,
        last_heartbeat=fixed_income_feed.last_heartbeat,
        now=now,
        stale_after=stale_after,
        reconnect=_reconnecting(fixed_income_feed),
    )
    return status


processor = MarketProcessor()
# Clients subscribe to symbols from the feed's universe; a new
# subscription gets a snapshot of the processor's latest quotes.
gateway = WebSocketGateway(
    symbols=lambda: connector.symbols,
    snapshot=lambda symbols: {
        s: q for s in symbols if (q := processor.get_latest(s)) is not None
    },
    status=current_status,
    asset_class=asset_class,
)

# Queryable copy of the instrument master; re-seeded from
# data/instruments.json on every startup.
instrument_store = InstrumentStore(config.DB_PATH)
# The search bar's index, over the same instruments the store is seeded with.
instrument_search = InstrumentSearch(INSTRUMENTS.values())
# Company profiles, officers and financials for description pages;
# re-seeded from data/company_profiles.json on every startup.
company_store = CompanyStore(config.DB_PATH)

# The buffer decouples ingestion (connector) from its downstream
# consumers, each getting an independent bounded queue: drop-oldest for
# the live display, lossless (bigger, and briefly blocking the feed when
# full) for branches that must see every tick. See queue/market_buffer.py.
buffer = MarketDataBuffer(
    connector.stream(),
    maxsize=config.QUEUE_MAX_SIZE,
    lossless_maxsize=config.LOSSLESS_QUEUE_MAX_SIZE,
    block_timeout=config.LOSSLESS_BLOCK_SECONDS,
)

# Real-time delivery: every tick, validated so a bad tick never reaches
# a WebSocket client. A dropped tick here only delays a price update.
validated_feed = ValidatingStream(buffer.subscribe("processor"), name="processor")


def _shares_outstanding(symbol: str) -> Optional[int]:
    company = COMPANIES.get(symbol)
    return company.profile.shares_outstanding.value if company else None


# Market cap (VWAP x shares outstanding) set on each equity tick, so the
# quote cards and snapshots carry the calculated figure.
priced_feed = with_market_cap(validated_feed, _shares_outstanding)

# OHLCV persistence: a separate branch off the buffer, so a slow database
# write can never delay real-time delivery. Validates independently.
# Lossless: a lost tick can mean a wrong high, low or volume, so if one
# is dropped anyway the candles it touched are flagged. (The alert
# engine, when it lands, subscribes the same way.)
aggregator = MarketAggregator(
    buffer.subscribe("aggregator", lossless=True, on_drop=lambda tick: aggregator.mark_dropped(tick)),
    db_path=config.DB_PATH,
)

# Set once startup() creates it, so /health can check whether it's still
# alive (e.g. hasn't died from an unhandled exception in processor.consume).
consumer_task: Optional[asyncio.Task] = None
status_task: Optional[asyncio.Task] = None


def pipeline_metrics() -> dict:
    return metrics.collect(
        buffer=buffer,
        gateway=gateway,
        validators={"processor": validated_feed.rejections, "aggregator": aggregator.rejections},
        aggregator=aggregator,
        feeds=feeds,
    )


@app.get("/metrics")
async def get_metrics(format_: Literal["prometheus", "json"] = Query("prometheus", alias="format")):
    """Pipeline metrics: dropped ticks, queue depth and capacity per
    buffer subscriber (and how long the feed waited on lossless ones),
    candles flagged possibly incomplete, WebSocket clients, ticks rejected by validation (by
    rule), the aggregator's last flush, and feed reconnects. Prometheus
    text format by default, for scraping; ?format=json for the same
    numbers as JSON. See app/metrics.py."""
    snapshot = pipeline_metrics()
    if format_ == "json":
        return snapshot
    return PlainTextResponse(metrics.to_prometheus(snapshot), media_type="text/plain; version=0.0.4")


@app.get("/health")
async def health():
    components = {
        "connector": connector.running,
        "buffer": buffer.healthy,
        "processor": consumer_task is not None and not consumer_task.done(),
        "aggregator": aggregator.healthy,
        "fixed_income": fixed_income.ready,
    }
    healthy = all(components.values())
    if healthy:
        status = "healthy"
    elif any(f.reconnecting for f in feeds):
        status = "reconnecting"
    else:
        status = "unhealthy"
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": status,
            "components": components,
            # Each feed's supervisor: connected, or reconnecting (which
            # attempt, when the next is, why it dropped).
            "feeds": {f.name: f.describe() for f in feeds},
            # The headline numbers from /metrics.
            "metrics": metrics.summary(pipeline_metrics()),
        },
    )


@app.get("/instruments", response_model=list[Instrument])
async def list_instruments(
    asset_class: Optional[AssetClass] = None,
    sector: Optional[str] = None,
):
    """The instrument master, optionally filtered by asset class and/or
    sector (sector match is case-insensitive). Includes suspended and
    delisted instruments and ones the feed doesn't stream."""
    return await asyncio.to_thread(instrument_store.list, asset_class, sector)


@app.get("/instruments/{symbol}", response_model=Instrument)
async def instrument_detail(symbol: str):
    instrument = await asyncio.to_thread(instrument_store.get, symbol.upper())
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Unknown instrument '{symbol.upper()}'")
    return instrument


@app.get("/instruments/{symbol}/description")
async def instrument_description(symbol: str):
    """Everything an equity's description page needs, in one response:
    the instrument master entry, the company profile, officers (in
    display order) and annual financials (newest first) we maintain,
    and figures calculated from them and the live quote.

    Every maintained value is {value, source, as_of}; a field nobody has
    filled in yet is all null, never left out. Calculated figures carry
    their formula and their inputs' dates instead of a source; see
    app/company/figures.py. 404 for unknown symbols and for bills and
    bonds."""
    symbol = symbol.upper()
    instrument = await asyncio.to_thread(instrument_store.get, symbol)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Unknown instrument '{symbol}'")
    if instrument.asset_class != "equity":
        raise HTTPException(
            status_code=404, detail=f"'{symbol}' is a {instrument.asset_class}; description pages are for equities"
        )
    company = await asyncio.to_thread(company_store.get, symbol) or Company(symbol=symbol)
    quote = processor.get_latest(symbol)
    now = datetime.now(timezone.utc)
    daily = days(await aggregator.get_candles(symbol, "1d", now - FIGURES_HISTORY))
    return {
        "symbol": symbol,
        "instrument": instrument.model_dump(mode="json"),
        "profile": company.profile.model_dump(mode="json"),
        "officers": [o.model_dump(mode="json") for o in company.officers],
        "financials": [f.model_dump(mode="json") for f in company.financials],
        "calculated": calculated_figures(
            company, quote if isinstance(quote, MarketData) else None,
            daily=daily, index=await gse_ci(), today=now.date(),
        ),
    }


# Daily history the figures need: a year for the 52-week range, total
# return and beta, plus slack for the reference close before it.
FIGURES_HISTORY = timedelta(days=400)
INDEX_TTL_SECONDS = 60
_index_cache: Optional[tuple[float, list[Day]]] = None


async def gse_ci() -> list[Day]:
    """The simulated GSE-CI over FIGURES_HISTORY, from every equity's
    daily candles; rebuilt at most once a minute."""
    global _index_cache
    if _index_cache is not None and time_module.monotonic() - _index_cache[0] < INDEX_TTL_SECONDS:
        return _index_cache[1]
    start = datetime.now(timezone.utc) - FIGURES_HISTORY
    constituents = {s: days(await aggregator.get_candles(s, "1d", start)) for s in equities.symbols}
    series = simulated_gse_ci(constituents)
    _index_cache = (time_module.monotonic(), series)
    return series


@app.get("/indices/gse-ci")
async def gse_ci_series():
    """The simulated GSE Composite Index used for beta: daily levels
    from every equity's daily closes, equal-weighted, from 1,000. A
    stand-in until the feed carries the published index; see
    app/company/index.py."""
    series = await gse_ci()
    return {
        "name": INDEX_NAME,
        "method": "equal-weighted daily close-to-close returns of every listed equity, chained",
        "base_level": BASE_LEVEL,
        "series": [{"date": d.on.isoformat(), "level": d.close} for d in series],
    }


def _last_price(symbol: str) -> tuple[Optional[float], Optional[float]]:
    """(price, change) as the UI headlines them: an equity's session VWAP
    (the GSE closing price) and its change on the previous close; a bill
    or bond's closing price and its change on the open. None where
    there's no quote yet."""
    quote = processor.get_latest(symbol)
    if quote is None:
        return None, None
    if isinstance(quote, FixedIncomeTick):
        close, open_ = quote.closing_price, quote.opening_price
        change = None if close is None or open_ is None else round(close - open_, 4)
        return close, change
    return quote.vwap, quote.change


@app.get("/search")
async def search(
    q: str = "",
    limit: int = Query(10, ge=1, le=50),
    asset_class: Optional[AssetClass] = None,
    sector: Optional[str] = None,
):
    """Typeahead over the instrument master: symbol, name, ISIN, tenor,
    issuer and maturity date, case- and punctuation-insensitive. Ranked
    exact symbol, symbol prefix, name word prefix, then substring; see
    app/instruments/search.py. Optionally filtered by asset class and/or
    sector (case-insensitive). An empty query returns no results, unless
    a sector is given: then it lists that sector."""
    results = []
    for i in instrument_search.search(q, limit=limit, asset_class=asset_class, sector=sector):
        price, change = _last_price(i.symbol)
        results.append({
            "symbol": i.symbol,
            "name": i.name,
            "asset_class": i.asset_class,
            "price": price,
            "change": change,
        })
    return results


@app.get("/sectors")
async def sectors():
    """Every sector in the instrument master with its number of
    instruments, for the search bar's Sector filter."""
    return [{"sector": name, "count": n} for name, n in instrument_search.sectors()]


@app.get("/movers")
async def movers(
    type: MoverType,
    limit: int = Query(10, ge=1, le=50),
    sector: Optional[str] = None,
    q: str = "",
):
    """Today's top gainers, top losers or most active equities, from the
    live quotes; see app/processors/movers.py. Optionally only one sector
    (case-insensitive), and/or only instruments matching the search
    query `q`."""
    if q.strip() or sector:
        candidates = instrument_search.search(q, limit=None, asset_class="equity", sector=sector)
    else:
        candidates = [i for i in INSTRUMENTS.values() if i.asset_class == "equity"]
    quotes = [
        quote for i in candidates
        if isinstance(quote := processor.get_latest(i.symbol), MarketData)
    ]
    return [
        {
            "symbol": quote.symbol,
            "name": quote.name,
            "asset_class": "equity",
            "price": quote.vwap,
            "change": quote.change,
            "change_percent": change_percent(quote),
            "volume": quote.volume,
        }
        for quote in rank_movers(quotes, type, limit)
    ]


@app.get("/fixed-income/report", response_model=FixedIncomeReport)
async def fixed_income_report():
    """The whole GFIM-style daily report for the current session: every
    section plus the summary. Values are "so far" until the session
    closes; blank report cells are null."""
    return fixed_income.report()


@app.get("/fixed-income/summary", response_model=FixedIncomeSummary)
async def fixed_income_summary():
    """Volume and number of trades per section, with each section's
    largest trade -- the report's SUMMARY sheet."""
    return fixed_income.report().summary


@app.get("/fixed-income/{section}")
async def fixed_income_section(section: ReportSection):
    """One section's rows: new_gog, ddep, old_gog, treasury_bill,
    corporate or sell_buy_back."""
    return getattr(fixed_income.report(), section)


@app.get("/curve", response_model=GovernmentYieldCurve)
async def yield_curve(day: Optional[date] = Query(None, alias="date")):
    """The GHS Government of Ghana yield curve: (tenor in years, closing
    yield) points from bills and bonds, each with the instruments behind
    it. `date` (YYYY-MM-DD) picks the session -- the latest on or before
    it, so a weekend or holiday gives the session before -- and defaults
    to the current one, so far."""
    try:
        return fixed_income.curve(day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/market/status")
async def get_market_status():
    """Whether the exchange is in session (open | pre_open | closed, and
    why), the next open and close, the last close, and how fresh the
    feed is. `badge` is what the UI shows: live | delayed | closed |
    disconnected. Declared before /market/{symbol} so it isn't taken for
    a symbol."""
    return current_status()


@app.get("/market/{symbol}")
async def get_market(symbol: str):
    symbol = symbol.upper()
    if symbol not in connector.symbols:
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol}'")
    data = processor.get_latest(symbol)
    if data is None:
        # Tracked, but its opening snapshot hasn't come through yet.
        raise HTTPException(status_code=404, detail=f"No market data yet for '{symbol}'")
    return data


@app.get("/candles")
async def get_candles(
    symbol: str,
    range_: str = Query("1D", alias="range"),
    interval: Optional[str] = None,
):
    """OHLCV candles as JSON for charting. `range` is one of the chart
    selector ranges (1D, 5D, 1M, 6M, YTD, 1Y, 5Y, Max) and picks both the
    lookback and a suitable candle interval; `interval` overrides the
    latter. The newest candle is the live, still-open one. For bills and
    bonds OHLC is clean price and `yield` the same window in yield (null
    for equities and price-only corporates), so a chart can toggle.
    `possibly_incomplete` is true where the live feed lost a tick in
    that window under load.
    """
    symbol = symbol.upper()
    if symbol not in connector.symbols:
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol}'")

    spec = RANGES.get(range_)
    if spec is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown range '{range_}'. Expected one of: {', '.join(RANGES)}",
        )
    interval = interval or spec.interval
    if interval not in aggregator.intervals:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown interval '{interval}'. Expected one of: {', '.join(aggregator.intervals)}",
        )

    start = spec.start(datetime.now(timezone.utc))
    candles = await aggregator.get_candles(symbol, interval, start)
    return {
        "symbol": symbol,
        "range": range_,
        "interval": interval,
        "start": start.isoformat() if start else None,
        "candles": [
            {
                "time": c.window_start.isoformat(),
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
                "yield": None if c.yield_close is None else {
                    "open": c.yield_open,
                    "high": c.yield_high,
                    "low": c.yield_low,
                    "close": c.yield_close,
                },
                "possibly_incomplete": c.possibly_incomplete,
            }
            for c in candles
        ],
    }


@app.get("/candles/export")
async def export_candles(symbol: Optional[str] = None, interval: str = "15m"):
    """Historical OHLCV candles from market_candles as a CSV download --
    opens directly in Excel, or feed it to any charting/analysis tool.
    """

    def fetch_rows() -> list[tuple]:
        conn = sqlite3.connect(aggregator.db_path)
        try:
            query = (
                "SELECT symbol, interval, window_start, window_end, "
                "open, high, low, close, volume, tick_count, "
                "yield_open, yield_high, yield_low, yield_close, possibly_incomplete "
                "FROM market_candles WHERE interval = ?"
            )
            params: list = [interval]
            if symbol:
                query += " AND symbol = ?"
                params.append(symbol.upper())
            query += " ORDER BY symbol, window_start"
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()

    rows = await asyncio.to_thread(fetch_rows)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "symbol", "interval", "window_start", "window_end",
        "open", "high", "low", "close", "volume", "tick_count",
        "yield_open", "yield_high", "yield_low", "yield_close", "possibly_incomplete",
    ])
    writer.writerows(rows)

    filename = f"market_candles_{symbol.upper()}.csv" if symbol else "market_candles.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/fixed-income", response_class=HTMLResponse)
async def fixed_income_page():
    """The Fixed Income tab: bills and bonds by GFIM segment and
    sell/buy-backs, live over /ws/market. The curve is /yield-curve."""
    return (STATIC_DIR / "fixed_income.html").read_text(encoding="utf-8")


@app.get("/yield-curve", response_class=HTMLResponse)
async def yield_curve_page():
    """The Yield Curve tab: the GoG curve from /curve against the
    previous day, week or month, live over /ws/market."""
    return (STATIC_DIR / "yield_curve.html").read_text(encoding="utf-8")


@app.get("/bond/{symbol}/analytics")
async def bond_analytics(
    symbol: str,
    settle: Optional[date] = None,
    face: float = Query(1_000_000, gt=0),
    price: Optional[float] = Query(None, gt=0),
    yield_: Optional[float] = Query(None, alias="yield"),
):
    """The bond page's calculator (a simplified YAS; see
    app/bond_math/calculator.py): give a clean `price` (per 100) or a
    `yield` (% a year) and get the other, plus duration, convexity, DV01
    and the invoice for `face` nominal. `settle` (YYYY-MM-DD) defaults to
    T+2 on the GFIM calendar (T+0 if that isn't before maturity); with neither price nor yield, the yield is
    the latest closing yield (else the bid/ask mid). 422 for a security
    the v1 conventions don't price (corporates, GFSF and USD DDE bonds);
    409 when there is no quote to start from."""
    symbol = symbol.upper()
    tick = processor.get_latest(symbol) if symbol in fixed_income_connector.symbols else None
    if not isinstance(tick, FixedIncomeTick):
        raise HTTPException(status_code=404, detail=f"No quote for bill or bond '{symbol}'")
    try:
        sec = security_for(tick, INSTRUMENTS.get(symbol))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    trade_date = fixed_income_calendar.local_date(datetime.now(timezone.utc))
    default_settle = settlement_date(trade_date, fixed_income_calendar)
    if default_settle >= sec.maturity:
        # A bill in its last days can't settle T+2; GFIM allows T+0 bilaterally.
        default_settle = trade_date
    if price is None and yield_ is None:
        yield_ = quoted_yield(tick)
        if yield_ is None:
            raise HTTPException(status_code=409, detail=f"No quote yet for {symbol}; give a price or yield")
    try:
        calc = calculate(sec, settle or default_settle, face, price=price, yield_pct=yield_)
    except (ValueError, ArithmeticError) as e:
        raise HTTPException(status_code=400, detail=str(e))

    inv = calc.invoice
    return {
        "symbol": symbol,
        "name": tick.name,
        "kind": sec.kind,
        "currency": tick.currency,
        "maturity_date": sec.maturity.isoformat(),
        "coupon": sec.bond.coupon if sec.bond else 0.0,
        "frequency": sec.bond.frequency if sec.bond else 0,
        "day_count": sec.day_count,
        "trade_date": trade_date.isoformat(),
        "default_settlement_date": default_settle.isoformat(),
        "settlement_date": calc.settlement_date.isoformat(),
        "face": calc.face,
        "solved_for": calc.solved_for,
        "clean_price": calc.clean_price,
        "dirty_price": calc.dirty_price,
        "yield": calc.yield_pct,
        "previous_coupon_date": calc.previous_coupon.isoformat() if calc.previous_coupon else None,
        "next_coupon_date": calc.next_coupon.isoformat() if calc.next_coupon else None,
        "risk": {
            "macaulay_duration": calc.macaulay_duration,
            "modified_duration": calc.modified_duration,
            "convexity": calc.convexity,
            "dv01": calc.dv01,
            "position_dv01": calc.position_dv01,
        },
        "invoice": {
            "principal": inv.principal,
            "accrued": inv.accrued,
            "days_accrued": inv.days_accrued,
            "total": inv.total,
        },
    }


@app.get("/bond/{symbol}", response_class=HTMLResponse)
async def bond_page(symbol: str):
    """A bill or bond's detail page (a simplified YAS), opened from the
    Fixed Income tab: live quote, price/yield calculator, risk, invoice
    and history."""
    if symbol.upper() not in fixed_income_connector.symbols:
        raise HTTPException(status_code=404, detail=f"Unknown bill or bond '{symbol.upper()}'")
    return (STATIC_DIR / "bond.html").read_text(encoding="utf-8")


@app.get("/ticker", response_class=HTMLResponse)
async def ticker_page():
    """Live-updating quote card UI, driven by the same /ws/market feed."""
    return (STATIC_DIR / "ticker.html").read_text(encoding="utf-8")


@app.get("/static/search.js")
async def search_script():
    """The header search bar shared by /ticker and /stock/{symbol}."""
    return FileResponse(STATIC_DIR / "search.js", media_type="text/javascript")


@app.get("/stock")
async def stock_page_default():
    return RedirectResponse(url=f"/stock/{equities.symbols[0]}")


@app.get("/stock/{symbol}", response_class=HTMLResponse)
async def stock_page(symbol: str):
    """Single-stock detail page: live quote over /ws/market plus a
    range-selectable chart from /candles. The page reads the symbol from
    its own URL."""
    if symbol.upper() not in equities.symbols:  # bills and bonds get their own page (#39)
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol.upper()}'")
    return (STATIC_DIR / "stock.html").read_text(encoding="utf-8")


@app.websocket("/ws/market")
async def market_websocket(websocket: WebSocket):
    """Live quotes for the symbols a client subscribes to. See
    app/gateways/websocket_gateway.py for the protocol."""
    await gateway.serve(websocket)


async def consumer_loop() -> None:
    """Validated buffer feed -> processor -> gateway broadcast.

    If this crashes, /health's "processor" check picks it up via
    consumer_task.done() -- but that only tells you *that* it died, not
    *why*. Log the exception here so the cause isn't only visible if/when
    asyncio's default "Task exception was never retrieved" handler fires.
    """
    try:
        await processor.consume(priced_feed, on_processed=gateway.broadcast)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Market processor consume loop crashed")
        raise


async def status_loop() -> None:
    """Push the market status to every WebSocket client periodically, so
    badges flip at the open and close, and when the feed goes quiet,
    without waiting for a tick."""
    while True:
        await asyncio.sleep(config.STATUS_INTERVAL_SECONDS)
        try:
            await gateway.broadcast_status(current_status())
        except Exception:
            logger.exception("Market status broadcast failed")


@app.on_event("startup")
async def startup():
    global consumer_task, status_task
    await asyncio.to_thread(instrument_store.seed, INSTRUMENTS.values())
    await asyncio.to_thread(company_store.seed, COMPANIES.values())
    await connector.connect()
    # Before any live ticks flow, so history is in place (and open
    # windows seeded) by the time the aggregator starts recording.
    await aggregator.backfill(connector)
    await buffer.start()
    await aggregator.start()
    consumer_task = asyncio.create_task(consumer_loop())
    status_task = asyncio.create_task(status_loop())


@app.on_event("shutdown")
async def shutdown():
    for task in (status_task, consumer_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await aggregator.stop()
    await buffer.stop()
    await connector.disconnect()
