import asyncio
import csv
import io
import logging
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from app import config
from app.aggregation.market_aggregator import MarketAggregator
from app.aggregation.ranges import RANGES
from app.connectors.composite_connector import CompositeConnector
from app.connectors.fixed_income_connector import MockFixedIncomeConnector
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.connectors.market_connector import MockMarketConnector
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

# One feed for the pipeline: equities and fixed income ticks together.
connector = CompositeConnector([equities, fixed_income_connector])


def asset_class(symbol: str) -> str:
    if symbol in equities.symbols:
        return "equity"
    return fixed_income.asset_class(symbol)


def current_status() -> dict:
    """Session state plus feed freshness: what the UI's badge shows. The
    top level is the equity market's; `fixed_income` is the same for
    GFIM, whose session is longer, for the Fixed Income tab's badge."""
    now = datetime.now(timezone.utc)
    stale_after = timedelta(seconds=config.FEED_STALE_SECONDS)
    status = market_status(
        calendar,
        running=connector.running,
        last_heartbeat=connector.last_heartbeat,
        now=now,
        stale_after=stale_after,
    )
    status["fixed_income"] = market_status(
        fixed_income_calendar,
        running=fixed_income_connector.running,
        last_heartbeat=fixed_income_connector.last_heartbeat,
        now=now,
        stale_after=stale_after,
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

# The buffer decouples ingestion (connector) from its downstream
# consumers, each getting an independent bounded queue with drop-oldest
# backpressure. See queue/market_buffer.py for why.
buffer = MarketDataBuffer(connector.stream(), maxsize=config.QUEUE_MAX_SIZE)

# Real-time delivery: every tick, validated so a bad tick never reaches
# a WebSocket client.
validated_feed = ValidatingStream(buffer.subscribe("processor"), name="processor")

# OHLCV persistence: a separate branch off the buffer, so a slow database
# write can never delay real-time delivery. Validates independently.
aggregator = MarketAggregator(buffer.subscribe("aggregator"), db_path=config.DB_PATH)

# Set once startup() creates it, so /health can check whether it's still
# alive (e.g. hasn't died from an unhandled exception in processor.consume).
consumer_task: Optional[asyncio.Task] = None
status_task: Optional[asyncio.Task] = None


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
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "healthy" if healthy else "unhealthy",
            "components": components,
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
                "yield_open, yield_high, yield_low, yield_close "
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
        "yield_open", "yield_high", "yield_low", "yield_close",
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


@app.get("/bond/{symbol}", response_class=HTMLResponse)
async def bond_page(symbol: str):
    """A bill or bond's page, opened from the Fixed Income tab. For now a
    summary of its quote; the full detail page is #39."""
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
        await processor.consume(validated_feed, on_processed=gateway.broadcast)
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
