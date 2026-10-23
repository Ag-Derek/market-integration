import asyncio
import csv
import io
import logging
import math
import sqlite3
import time as time_module
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from pydantic import BaseModel, ConfigDict, Field
from QuantLib import __version__ as ql_version

from app import config, metrics
from app.aggregation.market_aggregator import MarketAggregator
from app.aggregation.ranges import MAX_WARMUP, RANGES
from app.analytics.graph import (
    DEFAULT_RANGE,
    GRAPH_RANGES,
    MAX_SERIES,
    MIN_OBSERVATIONS,
    Change as GraphChange,
    Frequency as GraphFrequency,
    GraphRange,
    Point as GraphPoint,
    correlation_matrix,
    rebase,
    weekly,
)
from app.analytics.spreads import spread_analysis
from app.auctions import BILL_TENORS, AuctionResult
from app.auctions.bog import PAGE_URL as AUCTION_SOURCE_URL, SOURCE as AUCTION_SOURCE, BogAuctionClient
from app.auctions.monitor import monitor as auction_monitor, result_out as auction_result_out
from app.auctions.store import AuctionStore
from app.auctions.tracker import RETRY_SECONDS as AUCTION_RETRY_SECONDS, AuctionTracker
from app.econ import load_mpc_meetings
from app.econ.bog import BogPolicyRateClient
from app.econ.events import calendar_events, policy_rate_now
from app.econ.gss import GssCalendarClient
from app.econ.store import EconStore
from app.econ.tracker import RETRY_SECONDS as ECON_RETRY_SECONDS, EconTracker
from app.bond_math import settlement_date
from app.bond_math.conventions import add_months
from app.bond_math.calculator import calculate, quoted_yield, security_for
from app.commodities import COMMODITIES, SOURCES as COMMODITY_SOURCES, Price, month_key, monthly_means, parse_month
from app.commodities.prices import RETRY_SECONDS as COMMODITY_RETRY_SECONDS, CommodityPrices
from app.commodities.store import CommodityStore
from app.commodities.worldbank import WorldBankClient
from app.company import COMPANIES
from app.company.figures import INDEX_NAME, calculated_figures, with_market_cap
from app.company.index import BASE_LEVEL, simulated_gse_ci
from app.company.performance import Day, days
from app.company.seasonality import seasonality
from app.company.store import CompanyStore
from app.connectors.composite_connector import CompositeConnector
from app.connectors.depository import Depository, EmailDepository, MockDepository, OutboxSender, SmtpSender
from app.connectors.fixed_income_connector import MockFixedIncomeConnector
from app.connectors.fixed_income_mock import MockFixedIncomeMarket
from app.connectors.load_connector import LoadConnector
from app.connectors.market_connector import MockMarketConnector
from app.connectors.supervisor import SupervisedConnector
from app.fx import BASE as FX_BASE, BASE_NAME as FX_BASE_NAME, BASE_SYMBOL as FX_BASE_SYMBOL, CURRENCIES
from app.fx.bog import HISTORY_PAGE_URL as FX_SOURCE_URL, SOURCE as FX_SOURCE, BankOfGhanaClient
from app.derivatives.fx_option import (
    VOL_WINDOWS,
    Market as OptionMarket,
    OptionError,
    forward as fx_forward,
    historical_vol,
    implied_vol as option_implied_vol,
    likely_range,
    value as option_value,
    values_across as option_values_across,
    window_for as vol_window_for,
)
from app.fx.forwards import GHS_BASIS, TENORS as FORWARD_TENORS, cross_matrix, forward_curve, interpolate
from app.fx.overnight import (
    BENCHMARKS as OVERNIGHT_BENCHMARKS,
    RETRY_SECONDS as OVERNIGHT_RETRY_SECONDS,
    CentralBankClient,
    OvernightRates,
)
from app.fx.rates import RETRY_SECONDS as FX_RETRY_SECONDS, FxRates
from app.fx.store import FxStore, OvernightStore
from app.gateways.notification_gateway import NotificationGateway
from app.gateways.websocket_gateway import WebSocketGateway
from app.identity import current_user_id, set_user_cookie, valid_user_id, websocket_user_id
from app.instruments import INSTRUMENTS
from app.instruments.search import InstrumentSearch
from app.instruments.store import InstrumentStore
from app.models.fixed_income import (
    FixedIncomeReport,
    FixedIncomeSummary,
    FixedIncomeTick,
    GovernmentYieldCurve, ReportSection,
    SpreadAnalysis,
)
from app.models.auction import AuctionMonitor, AuctionResultOut
from app.models.company import Company
from app.models.econ import EconCalendar, PolicyRatePoint
from app.models.candle import INTERVALS, Candle
from app.models.fx import FxRate
from app.models.instrument import AssetClass, Instrument
from app.models.market_data import MarketData
from app.models.ticket import Notification, Ticket, TicketConfirmation, TicketRequest, TicketStatus
from app.processors.market_processor import MarketProcessor
from app.processors.movers import MoverType, change_percent, rank_movers
from app.queue.market_buffer import MarketDataBuffer
from app.session.calendar import load_calendar
from app.session.status import market_status
from app.tickets import TicketError
from app.tickets.desk import TicketDesk
from app.tickets.pdf import render_ticket_pdf
from app.tickets.store import TicketStore
from app.validation.market_validator import ValidatingStream
from app.watchlists import DEFAULT_WATCHLIST, MAX_PINS, WatchlistError, validate as validate_watchlist
from app.watchlists.store import WatchlistStore
from app.drawings.store import DrawingStore
from app.models.drawing import MAX_DRAWINGS, DrawingsUpdate

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
    # One batched message per client at most this often, whatever the
    # tick rate (#20).
    send_interval=config.WS_SEND_INTERVAL_MS / 1000,
)

# Queryable copy of the instrument master; re-seeded from
# data/instruments.json on every startup.
instrument_store = InstrumentStore(config.DB_PATH)
# The search bar's index, over the same instruments the store is seeded with.
instrument_search = InstrumentSearch(INSTRUMENTS.values())
# Company profiles, officers and financials for description pages;
# re-seeded from data/companies/ (one file per company) on every startup.
company_store = CompanyStore(config.DB_PATH)
# Each user's pinned equities (#26). User data: kept across restarts,
# never re-seeded.
watchlist_store = WatchlistStore(config.DB_PATH)
# Each user's drawings on each stock chart (trend lines, levels,
# retracements). User data: kept across restarts, never re-seeded.
drawing_store = DrawingStore(config.DB_PATH)
# Bank of Ghana interbank FX fixes for showing prices in other
# currencies: pulled from BoG's site and kept across restarts; see app/fx/.
fx_rates = FxRates(FxStore(config.DB_PATH), BankOfGhanaClient())
# SOFR, €STR and SONIA, the foreign legs of synthetic forwards: pulled
# from the NY Fed, ECB and BoE and kept across restarts; see app/fx/overnight.py.
overnight_rates = OvernightRates(OvernightStore(config.DB_PATH), CentralBankClient())
# Bank of Ghana T-bill auction results for the new-issue monitor: pulled
# from BoG's site and kept across restarts; see app/auctions/.
auctions = AuctionTracker(AuctionStore(config.DB_PATH), BogAuctionClient())
# The economic calendar: BoG policy rate history and GSS's release
# calendar (pulled, kept across restarts), MPC meeting dates (seed file).
econ = EconTracker(EconStore(config.DB_PATH), BogPolicyRateClient(), GssCalendarClient())
mpc_meetings = load_mpc_meetings(config.ECON_CALENDAR_PATH)
# Monthly commodity reference prices (Brent, gold, cocoa, ...): pulled
# from the World Bank's Pink Sheet and kept across restarts; see
# app/commodities/.
commodity_prices = CommodityPrices(CommodityStore(config.DB_PATH), [WorldBankClient()])
def _depository() -> Depository:
    """Per DEPOSITORY_MODE: by email (through SMTP, or saved to the
    outbox to send by hand), or the mock."""
    if config.DEPOSITORY_MODE == "mock":
        return MockDepository()
    sender = (
        SmtpSender(config.SMTP_HOST, config.SMTP_PORT, config.SMTP_USERNAME, config.SMTP_PASSWORD)
        if config.SMTP_HOST else OutboxSender(config.DEPOSITORY_OUTBOX)
    )
    return EmailDepository(
        config.DEPOSITORY_EMAIL, config.MAIL_FROM or "tickets@localhost", sender, cc=config.DEPOSITORY_EMAIL_CC,
    )


# Trade tickets filed with the depository, and notifications to both
# parties (app/tickets/). User data, kept across restarts.
notification_gateway = NotificationGateway()
ticket_desk = TicketDesk(
    TicketStore(config.DB_PATH),
    _depository(),
    notification_gateway,
    instruments=INSTRUMENTS,
    calendar=calendar,
    today=lambda: calendar.local_date(datetime.now(timezone.utc)),
)

# The buffer decouples ingestion (connector) from its downstream
# consumers: conflated (latest tick per symbol) for the live display,
# lossless queues (blocking the feed briefly when full) for branches that
# must see every tick. See queue/market_buffer.py.
buffer = MarketDataBuffer(
    connector.stream(),
    maxsize=config.QUEUE_MAX_SIZE,
    lossless_maxsize=config.LOSSLESS_QUEUE_MAX_SIZE,
    block_timeout=config.LOSSLESS_BLOCK_SECONDS,
)

# Real-time delivery: the latest tick per symbol, conflated (#20). Screens
# and quote APIs only show the current price, so ticks superseded before
# the processor reads them are skipped rather than queued: a burst never
# backs up this branch. Validated so a bad tick never reaches a client.
validated_feed = ValidatingStream(buffer.subscribe_latest("processor"), name="processor")


def _shares_outstanding(symbol: str) -> Optional[int]:
    company = COMPANIES.get(symbol)
    return company.profile.shares_outstanding.value if company else None


# Market cap (VWAP x shares outstanding) set on each equity tick, so the
# quote cards and snapshots carry the calculated figure.
priced_feed = with_market_cap(validated_feed, _shares_outstanding)

# OHLCV persistence: a separate branch off the buffer, unconflated, so a
# slow database write can never delay real-time delivery and every tick
# reaches the candles. Validates independently.
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
fx_task: Optional[asyncio.Task] = None
overnight_task: Optional[asyncio.Task] = None
auction_task: Optional[asyncio.Task] = None
econ_task: Optional[asyncio.Task] = None
commodity_task: Optional[asyncio.Task] = None


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
            # Bank of Ghana FX rates: the latest fix held and the last
            # pull. Not a component: prices still flow without them, and
            # pages then show cedis.
            "fx": fx_rates.describe(),
            # The overnight benchmarks behind synthetic forwards, likewise.
            "overnight_rates": overnight_rates.describe(),
            # Bank of Ghana auction results, likewise: the latest issue
            # held and the last pull.
            "auctions": auctions.describe(),
            # The economic calendar's pulled sources (BoG policy rate, GSS).
            "economic_calendar": econ.describe(),
            # Commodity reference prices, likewise: the latest month held
            # and each source's last pull.
            "commodities": commodity_prices.describe(),
        },
    )


class WatchlistUpdate(BaseModel):
    """PUT /watchlists/me: the whole list, in order."""
    model_config = ConfigDict(extra="forbid")

    symbols: list[str]


def _watchlist_body(user_id: str, saved: Optional[dict]) -> dict:
    return {
        "user_id": user_id,
        "symbols": saved["symbols"] if saved else list(DEFAULT_WATCHLIST),
        # A user who has never saved one sees the default until they do.
        "is_default": saved is None,
        "updated_at": saved["updated_at"] if saved else None,
        "max_pins": MAX_PINS,
    }


@app.get("/watchlists/me")
async def get_my_watchlist(user_id: str = Depends(current_user_id)):
    """The requesting user's pinned equities, in order (#26): the saved
    list, or the default for a user who hasn't saved one. The user is
    the X-User-Id header or the anonymous-ID cookie, issued here on a
    first visit (app/identity.py)."""
    saved = await asyncio.to_thread(watchlist_store.get, user_id)
    return _watchlist_body(user_id, saved)


@app.put("/watchlists/me")
async def put_my_watchlist(update: WatchlistUpdate, user_id: str = Depends(current_user_id)):
    """Replace the requesting user's watchlist with `symbols`, in that
    order. 422 for unknown symbols, bills and bonds, duplicates or more
    than max_pins, naming the offending symbols; nothing is saved then."""
    try:
        symbols = validate_watchlist(update.symbols)
    except WatchlistError as e:
        raise HTTPException(status_code=422, detail={"message": str(e), "symbols": e.symbols})
    saved = await asyncio.to_thread(watchlist_store.put, user_id, symbols)
    return _watchlist_body(user_id, saved)


def _drawing_symbol(symbol: str) -> str:
    """An equity's symbol, upper-cased; 404 for anything else (drawings
    are for the stock page's chart)."""
    symbol = symbol.strip().upper()
    instrument = INSTRUMENTS.get(symbol)
    if instrument is None or instrument.asset_class != "equity":
        raise HTTPException(status_code=404, detail=f"No stock chart for '{symbol}'")
    return symbol


def _drawings_body(symbol: str, saved: Optional[dict]) -> dict:
    return {
        "symbol": symbol,
        "currency": FX_BASE,   # prices in the anchors
        "drawings": saved["drawings"] if saved else [],
        "updated_at": saved["updated_at"] if saved else None,
        "max_drawings": MAX_DRAWINGS,
    }


@app.get("/drawings/me")
async def get_my_drawn_charts(user_id: str = Depends(current_user_id)):
    """The charts the requesting user has drawn on: symbol -> how many
    drawings."""
    return {"charts": await asyncio.to_thread(drawing_store.symbols, user_id)}


@app.get("/drawings/me/{symbol}")
async def get_my_drawings(symbol: str, user_id: str = Depends(current_user_id)):
    """The requesting user's drawings on `symbol`'s chart: trend lines,
    levels and Fibonacci retracements, each as (time, price in cedis)
    anchors. Empty if they haven't drawn on it. The user is as for
    watchlists (app/identity.py). 404 for a symbol that isn't an equity."""
    symbol = _drawing_symbol(symbol)
    return _drawings_body(symbol, await asyncio.to_thread(drawing_store.get, user_id, symbol))


@app.put("/drawings/me/{symbol}")
async def put_my_drawings(symbol: str, update: DrawingsUpdate, user_id: str = Depends(current_user_id)):
    """Replace the requesting user's drawings on `symbol` with
    `drawings` (an empty list clears them). 422 for a malformed drawing
    (wrong number of points, a vertical trend line, a flat retracement,
    repeated ids, more than max_drawings); nothing is saved then."""
    symbol = _drawing_symbol(symbol)
    drawings = [d.model_dump(mode="json") for d in update.drawings]
    saved = await asyncio.to_thread(drawing_store.put, user_id, symbol, drawings)
    return _drawings_body(symbol, saved)


class IdentityUpdate(BaseModel):
    """PUT /identity/me."""
    model_config = ConfigDict(extra="forbid")

    user_id: str


@app.get("/identity/me")
async def get_identity(user_id: str = Depends(current_user_id)):
    """Who the requesting user is (app/identity.py): the name put on
    tickets they write and the one their counterparties type."""
    return {"user_id": user_id}


@app.put("/identity/me")
async def put_identity(update: IdentityUpdate, request: Request, response: Response):
    """Use `user_id` for this browser from now on (a cookie). A stand-in
    until Symphony sign-in: it is no more trusted than X-User-Id. 400 for
    an ID that isn't 1-128 letters, digits or . _ @ : -."""
    user_id = update.user_id.strip()
    if not valid_user_id(user_id):
        raise HTTPException(status_code=400, detail="A user ID is 1-128 letters, digits or . _ @ : -")
    set_user_cookie(request, response, user_id)
    return {"user_id": user_id}


async def _ticket_call(call):
    try:
        return await call
    except TicketError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page():
    """The Trade Tickets window: write a ticket, file it, print it and
    follow it to the depository's reply, with live notifications. Opened
    from the Fixed Income tab and bond pages with ?symbol=, &price= and
    &quantity= to start a ticket for that security."""
    return (STATIC_DIR / "tickets.html").read_text(encoding="utf-8")


@app.post("/tickets/preview", response_model=Ticket)
async def preview_ticket(request: TicketRequest, user_id: str = Depends(current_user_id)):
    """What POST /tickets would write, without saving it: settlement
    date and amounts for the form to show while it is filled in. Same
    422s as POST /tickets."""
    try:
        return ticket_desk.preview(request, user_id)
    except TicketError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))


@app.post("/tickets", response_model=Ticket, status_code=201)
async def create_ticket(request: TicketRequest, user_id: str = Depends(current_user_id)):
    """Write up a trade the requesting user (the trader) agreed with
    `counterparty`, as a draft. The instrument, settlement date and
    amounts are filled in: equities settle T+EQUITY_SETTLEMENT_LAG, bills
    and bonds T+2 with the bond calculator's invoice. 422 for an unknown
    or inactive instrument, a future or non-trading trade date,
    fractional shares, or a bill or bond the bond math can't price. See
    app/tickets/."""
    return await _ticket_call(ticket_desk.create(request, user_id))


@app.get("/tickets/me", response_model=list[Ticket])
async def my_tickets(status: Optional[TicketStatus] = None, user_id: str = Depends(current_user_id)):
    """Tickets the requesting user is a party to, as trader or
    counterparty, newest first; optionally only those in one status."""
    return await ticket_desk.list(user_id, status)


@app.get("/tickets/{ticket_id}", response_model=Ticket)
async def get_ticket(ticket_id: str, user_id: str = Depends(current_user_id)):
    """One ticket, for its trader or counterparty; 404 for anyone else."""
    return await _ticket_call(ticket_desk.get(ticket_id, user_id))


@app.post("/tickets/{ticket_id}/file", response_model=Ticket)
async def file_ticket(ticket_id: str, user_id: str = Depends(current_user_id)):
    """Send a draft ticket to the depository. Directly, it comes back
    filed (with the depository's reference) or rejected (with its
    reason); by email (DEPOSITORY_MODE=email), it comes back sent, the
    PDF emailed, until the depository's reply is recorded at
    /tickets/{id}/confirmation. Both parties are notified at each step.
    403 unless the requesting user is the ticket's trader; 409 if it
    isn't a draft; 503 if the depository or mail server can't be reached
    (it stays a draft, to send again)."""
    return await _ticket_call(ticket_desk.file(ticket_id, user_id))


@app.post("/tickets/{ticket_id}/confirmation", response_model=Ticket)
async def confirm_ticket(
    ticket_id: str, confirmation: TicketConfirmation, user_id: str = Depends(current_user_id),
):
    """Record the depository's reply to an emailed ticket: {"accepted":
    true, "reference": ...} files it, {"accepted": false, "reason": ...}
    rejects it; both parties are notified. 403 unless the requesting user
    is the ticket's trader; 409 unless the ticket is sent (waiting for
    the reply)."""
    return await _ticket_call(ticket_desk.confirm(ticket_id, user_id, confirmation))


@app.get("/tickets/{ticket_id}/pdf")
async def ticket_pdf(ticket_id: str, download: bool = False, user_id: str = Depends(current_user_id)):
    """The ticket as a printable A4 PDF, as it stands now: the same
    document the depository is emailed. Opens in the browser to print;
    `download=true` saves it instead. For the trader and counterparty
    only."""
    ticket = await _ticket_call(ticket_desk.get(ticket_id, user_id))
    pdf = await asyncio.to_thread(render_ticket_pdf, ticket)
    disposition = "attachment" if download else "inline"
    return Response(
        content=pdf, media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{ticket.id}.pdf"'},
    )


@app.get("/notifications/me", response_model=list[Notification])
async def my_notifications(unread: bool = False, user_id: str = Depends(current_user_id)):
    """The requesting user's ticket notifications, newest first;
    `unread=true` for only those not yet marked read. The same ones are
    pushed live over /ws/notifications."""
    return await asyncio.to_thread(ticket_desk.store.notifications, user_id, unread)


@app.post("/notifications/me/{notification_id}/read", status_code=204)
async def read_notification(notification_id: int, user_id: str = Depends(current_user_id)):
    """Mark one of the requesting user's notifications read; 404 if they
    have none with that ID."""
    if not await asyncio.to_thread(ticket_desk.store.mark_read, user_id, notification_id):
        raise HTTPException(status_code=404, detail=f"No notification {notification_id}")


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
    the instrument master entry and its related securities, the company
    profile, officers (in display order) and annual financials (newest
    first) we maintain, and figures calculated from them and the live
    quote.

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
    related = [INSTRUMENTS[s] for s in instrument.related_symbols if s in INSTRUMENTS]
    return {
        "symbol": symbol,
        "instrument": instrument.model_dump(mode="json"),
        # Other listed lines of the same issuer, for the Issue Info tab's links.
        "related": [{"symbol": r.symbol, "name": r.name, "kind": r.kind, "status": r.status} for r in related],
        "profile": company.profile.model_dump(mode="json"),
        "officers": [o.model_dump(mode="json") for o in company.officers],
        "financials": [f.model_dump(mode="json") for f in company.financials],
        "calculated": calculated_figures(
            company, quote if isinstance(quote, MarketData) else None,
            daily=daily, index=await gse_ci(), today=now.date(),
        ),
    }


@app.get("/seasonality/{symbol}")
async def get_seasonality(symbol: str):
    """An equity's seasonality (Bloomberg's SEAG) from every stored daily
    close, in cedis: each month's return (`months`) and year's
    (`annual`); per calendar month the mean, median, how many were up and
    the best and worst year (`by_month`); each year's path from
    1 January (`paths`) and the finished years' average path
    (`average`). The current month and year are to date, flagged
    `partial` and left out of the averages; see app/company/seasonality.py.
    404 for unknown symbols and for bills and bonds."""
    symbol = symbol.upper()
    instrument = INSTRUMENTS.get(symbol)
    if instrument is None:
        raise HTTPException(status_code=404, detail=f"Unknown instrument '{symbol}'")
    if instrument.asset_class != "equity":
        raise HTTPException(
            status_code=404, detail=f"'{symbol}' is a {instrument.asset_class}; seasonality is for equities"
        )
    series = days(await aggregator.get_candles(symbol, "1d")) if symbol in connector.symbols else []
    return {"symbol": symbol, "currency": FX_BASE, **seasonality(series, datetime.now(timezone.utc).date())}


# Daily history the figures need: a year for the 52-week range, total
# return and beta, plus slack for the reference close before it.
FIGURES_HISTORY = timedelta(days=400)
INDEX_TTL_SECONDS = 60
# history -> (built at, levels)
_index_cache: dict[Optional[timedelta], tuple[float, list[Day]]] = {}


async def gse_ci(history: Optional[timedelta] = FIGURES_HISTORY) -> list[Day]:
    """The simulated GSE-CI over `history` (None: every stored day, for
    /graph), from every equity's daily candles; rebuilt at most once a
    minute. It is 1,000 on its first day, so the levels depend on
    `history`; returns don't."""
    cached = _index_cache.get(history)
    if cached is not None and time_module.monotonic() - cached[0] < INDEX_TTL_SECONDS:
        return cached[1]
    start = None if history is None else datetime.now(timezone.utc) - history
    constituents = {s: days(await aggregator.get_candles(s, "1d", start)) for s in equities.symbols}
    series = simulated_gse_ci(constituents)
    _index_cache[history] = (time_module.monotonic(), series)
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


# ---------------------------------------------------------------- /graph

GRAPH_INDEX_ID = "GSE-CI"
GRAPH_FX_PREFIX = "FX:"
GRAPH_COMMODITY_PREFIX = "CMD:"
# Read this far ahead of a range's start, so a series has a value to
# rebase on when the start falls on a weekend, a holiday or a day a thin
# stock didn't trade.
GRAPH_BASE_SLACK = timedelta(days=14)


def _graph_ids(series: str) -> list[str]:
    """The comma-separated `series` parameter: ids upper-cased, in
    order, duplicates dropped. 400 for none or more than MAX_SERIES."""
    ids = list(dict.fromkeys(s.strip().upper() for s in series.split(",") if s.strip()))
    if not ids:
        raise HTTPException(status_code=400, detail="Give at least one series")
    if len(ids) > MAX_SERIES:
        raise HTTPException(status_code=400, detail=f"At most {MAX_SERIES} series on one graph, got {len(ids)}")
    return ids


def _graph_range(range_: str) -> GraphRange:
    spec = GRAPH_RANGES.get(range_)
    if spec is None:
        raise HTTPException(
            status_code=400, detail=f"Unknown range '{range_}'. Expected one of: {', '.join(GRAPH_RANGES)}",
        )
    return spec


def _short_label(instrument: Instrument) -> str:
    """A bill or bond as a legend can fit it: "GOG 8.65% Feb 2029",
    "GOG bill 05 Oct 26", rather than the GFIM description."""
    issuer = instrument.name.split("-")[0]
    maturity = instrument.maturity_date
    if maturity is None:
        return instrument.symbol
    if instrument.asset_class == "bill":
        return f"{issuer} bill {maturity:%d %b %y}"
    coupon = f" {instrument.coupon_rate:g}%" if instrument.coupon_rate else ""
    return f"{issuer}{coupon} {maturity:%b %Y}"


async def _graph_series(series_id: str, since: Optional[date]) -> tuple[dict, list[GraphPoint], GraphChange]:
    """(description, daily points from `since`, how it changes) for one
    /graph series id: an equity symbol (its daily closes, GHS), a bill or
    bond ISIN (its closing yield; price for price-only corporates),
    GSE-CI (the simulated index), FX:<code> (the Bank of Ghana mid, in
    cedis per unit) or CMD:<code> (a commodity's monthly average in US
    dollars, one point a month, dated the 1st). 404 for an unknown id;
    400/503 for an FX or commodity code we don't offer or have no prices
    for yet."""
    start = None if since is None else datetime.combine(since, time(), tzinfo=timezone.utc)
    if series_id == GRAPH_INDEX_ID:
        levels = [d for d in await gse_ci(None) if since is None or d.on >= since]
        meta = {"label": GRAPH_INDEX_ID, "name": f"{INDEX_NAME} (simulated, equal-weighted)",
                "kind": "index", "unit": "points"}
        return meta, [GraphPoint(d.on, d.close) for d in levels], "return"

    if series_id.startswith(GRAPH_FX_PREFIX):
        code = _fx_code(series_id[len(GRAPH_FX_PREFIX):])
        if code == FX_BASE:
            raise HTTPException(status_code=400, detail="The cedi is the base currency; pick another")
        name = CURRENCIES[code][0]
        meta = {"label": f"{code}/{FX_BASE}", "name": f"{name}, cedis per {code} (Bank of Ghana mid)",
                "kind": "fx", "unit": f"GHS per {code}"}
        return meta, [GraphPoint(r.date, r.mid) for r in fx_rates.history(code, since)], "return"

    if series_id.startswith(GRAPH_COMMODITY_PREFIX):
        code = _commodity_code(series_id[len(GRAPH_COMMODITY_PREFIX):])
        c = COMMODITIES[code]
        # From the month `since` falls in, so the range's start has a base.
        start_month = None if since is None else since.replace(day=1)
        meta = {"label": c.name, "name": f"{c.name}, US$ per {c.per} (monthly average)",
                "kind": "commodity", "unit": f"US$ per {c.per}"}
        return meta, [GraphPoint(p.month, p.value) for p in commodity_prices.history(code, start_month)], "return"

    instrument = INSTRUMENTS.get(series_id)
    if instrument is None or series_id not in connector.symbols:
        raise HTTPException(status_code=404, detail=f"Unknown series '{series_id}'")
    candles = await aggregator.get_candles(series_id, "1d", start)
    if asset_class(series_id) == "equity":
        meta = {"label": series_id, "name": instrument.name, "kind": "equity", "unit": FX_BASE}
        return meta, [GraphPoint(c.window_start.date(), c.close, c.volume > 0) for c in candles], "return"
    label = _short_label(instrument)
    if candles and all(c.yield_close is not None for c in candles):
        meta = {"label": label, "name": f"{instrument.name} ({series_id}), closing yield",
                "kind": "yield", "unit": "% a year"}
        points = [GraphPoint(c.window_start.date(), c.yield_close, c.volume > 0) for c in candles]
        return meta, points, "difference"
    meta = {"label": label, "name": f"{instrument.name} ({series_id}), clean price",
            "kind": "price", "unit": "per 100"}
    return meta, [GraphPoint(c.window_start.date(), c.close, c.volume > 0) for c in candles], "return"


async def _graph_inputs(ids: list[str], spec: GraphRange) -> tuple[Optional[date], list]:
    """The range's start and, per id, (description, points, change)."""
    today = datetime.now(timezone.utc).date()
    start = spec.start(today)
    since = None if start is None else start - GRAPH_BASE_SLACK
    return start, [(i, *await _graph_series(i, since)) for i in ids]


@app.get("/graph/series")
async def graph_series(series: str, range_: str = Query(DEFAULT_RANGE, alias="range")):
    """Up to MAX_SERIES series on one chart, each rebased to 100 at the
    range's start (Bloomberg's G); see app/analytics/graph.py. `series`
    is comma-separated ids: equity symbols (daily close, GHS), bill and
    bond ISINs (closing yield; clean price for price-only corporates),
    GSE-CI (the simulated index), FX:USD etc. (Bank of Ghana mid,
    cedis per unit) and CMD:GOLD etc. (commodity monthly averages, US$;
    see /commodities/prices). `range` is 1M, 3M, 6M, YTD, 1Y, 2Y, 5Y or Max; 5Y
    and Max give one point a week. Each point is [date, value, rebased].
    400 for a bad range or too many series, 404 for an unknown id."""
    spec = _graph_range(range_)
    start, inputs = await _graph_inputs(_graph_ids(series), spec)
    out = []
    for series_id, meta, points, kind in inputs:
        shown = rebase(points, start)
        rows = [] if shown is None else shown["points"]
        if spec.weekly:
            rows = weekly(rows)
        in_range = [p for p in points if start is None or p.on >= start]
        out.append({
            "id": series_id,
            **meta,
            "change": kind,
            "base": shown["base"] if shown else None,
            "late_start": shown["late_start"] if shown else False,
            "last": None if not rows else {"date": rows[-1][0], "value": rows[-1][1], "rebased": rows[-1][2]},
            # How thinly it trades: days with a print, of days with a value.
            "observed_days": sum(p.observed for p in in_range),
            "days": len(in_range),
            "points": rows,
        })
    return {
        "range": range_,
        "start": start.isoformat() if start else None,
        "step": "1w" if spec.weekly else "1d",
        "series": out,
    }


@app.get("/graph/correlation")
async def graph_correlation(
    series: str,
    range_: str = Query(DEFAULT_RANGE, alias="range"),
    frequency: GraphFrequency = "daily",
):
    """The correlation matrix (Bloomberg's CORR) of the /graph series
    over `range`: Pearson correlation of each pair's changes (returns;
    change in yield for yields), measured between the days both were
    observed, so a thin stock's no-trade days don't dilute it.
    `frequency=weekly` correlates week-on-week changes instead. Each
    cell is {value, observations}; value is null below
    min_observations. Same ids, ranges and errors as /graph/series."""
    start, inputs = await _graph_inputs(_graph_ids(series), _graph_range(range_))
    matrix = correlation_matrix([(points, kind) for _, _, points, kind in inputs], start, frequency)
    return {
        "range": range_,
        "start": start.isoformat() if start else None,
        "frequency": frequency,
        "min_observations": MIN_OBSERVATIONS,
        "series": [{"id": i, "label": meta["label"], "name": meta["name"], "change": kind}
                   for i, meta, _, kind in inputs],
        "matrix": matrix,
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


def _fx_code(code: str) -> str:
    """A currency we convert to, upper-cased. 400 if we don't offer it;
    503 if we do but have no Bank of Ghana rate for it yet."""
    code = code.strip().upper()
    if code != FX_BASE and code not in CURRENCIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown currency '{code}'. Expected one of: {', '.join([FX_BASE, *CURRENCIES])}",
        )
    if not fx_rates.has(code):
        raise HTTPException(status_code=503, detail=f"No Bank of Ghana rate for {code} yet; try again shortly")
    return code


def _require_fx() -> None:
    """503 until the first pull from BoG has landed."""
    if not fx_rates.currencies():
        raise HTTPException(
            status_code=503,
            detail={"message": "No Bank of Ghana rates yet; the first pull may still be running", **fx_rates.describe()},
        )


def _fix_on(code: str, day: Optional[date]) -> Optional[FxRate]:
    """The fix for `code` in force on `day` (latest if None), or None if
    the history held doesn't reach back to `day`."""
    rate = fx_rates.latest(code) if day is None else fx_rates.on(code, day)
    return None if rate is None or (day is not None and rate.date > day) else rate


@app.get("/fx/rates")
async def get_fx_rates(day: Optional[date] = Query(None, alias="date")):
    """The Bank of Ghana interbank rates prices are converted at: for
    each currency, cedis per unit (`ghs_per_unit`) and units per cedi
    (`per_ghs`) at the mid, with BoG's buying and selling and the day it
    published them. The latest fix by default; `date` (YYYY-MM-DD) gives
    the fix in force that day (BoG publishes on business days only).
    503 until the first pull from BoG has landed; 404 for a date before
    the history held. Each row also has the fix before it
    (`previous_mid`, `previous_date`) and the move since (`change_percent`,
    in the cedis-per-unit rate: positive means the cedi weakened), null
    when the history starts there. See app/fx/."""
    _require_fx()
    rows = []
    for code in fx_rates.currencies():
        rate = _fix_on(code, day)
        if rate is None:
            continue
        prev = fx_rates.on(code, rate.date - timedelta(days=1))
        if prev is not None and prev.date >= rate.date:
            prev = None  # the history starts at this fix
        name, symbol, _ = CURRENCIES[code]
        rows.append({
            "code": code,
            "name": name,
            "symbol": symbol,
            "date": rate.date.isoformat(),
            "ghs_per_unit": rate.mid,
            "per_ghs": rate.per_ghs,
            "buying": rate.buying,
            "selling": rate.selling,
            "source_pair": rate.source_pair,
            "previous_date": prev.date.isoformat() if prev else None,
            "previous_mid": prev.mid if prev else None,
            "change_percent": (rate.mid / prev.mid - 1) * 100 if prev else None,
        })
    if not rows:
        raise HTTPException(status_code=404, detail=f"No Bank of Ghana rates held for {day}")
    return {
        "base": {"code": FX_BASE, "name": FX_BASE_NAME, "symbol": FX_BASE_SYMBOL},
        "source": FX_SOURCE,
        "source_url": FX_SOURCE_URL,
        "rate": "mid",
        "date": day.isoformat() if day else None,
        "as_of": max(r["date"] for r in rows),
        "currencies": rows,
        "status": fx_rates.describe(),
    }


@app.get("/fx/convert")
async def fx_convert(
    amount: float,
    to: str,
    from_: str = Query(FX_BASE, alias="from"),
    day: Optional[date] = Query(None, alias="date"),
):
    """`amount` of `from` (default GHS) in `to`, at the Bank of Ghana
    mid: the latest fix, or the one in force on `date`. Between two
    foreign currencies it goes through the cedi, as BoG quotes only
    against the cedi. 400 for a currency we don't offer, 503 before the
    first pull from BoG, 404 for a date before the history held."""
    codes = (_fx_code(from_), _fx_code(to))
    fixes = {}
    for code in codes:
        if code == FX_BASE:
            continue
        rate = _fix_on(code, day)
        if rate is None:
            raise HTTPException(status_code=404, detail=f"No Bank of Ghana rate for {code} on or before {day}")
        fixes[code] = rate
    per_ghs = [fixes[c].per_ghs if c in fixes else 1.0 for c in codes]
    rate = per_ghs[1] / per_ghs[0]
    return {
        "amount": amount,
        "from": codes[0],
        "to": codes[1],
        "rate": rate,
        "result": amount * rate,
        "date": day.isoformat() if day else None,
        # The day each fix used was published, by currency.
        "fixes": {c: r.date.isoformat() for c, r in fixes.items()},
        "source": FX_SOURCE,
    }


@app.get("/fx/cross")
async def fx_cross(
    currencies: Optional[str] = Query(None, description="Comma-separated codes; default the cedi and every currency held"),
    day: Optional[date] = Query(None, alias="date"),
):
    """The cross-rate matrix at the Bank of Ghana mid: `matrix[i][j]` is
    units of currencies[j] one unit of currencies[i] buys. BoG fixes
    against the cedi only, so every cross goes through it (see
    app/fx/forwards.py). The latest fix by default, or the one in force on
    `date`. 400 for a currency we don't offer, 503 before the first pull
    from BoG, 404 for a date before the history held."""
    _require_fx()
    if currencies:
        codes = list(dict.fromkeys(_fx_code(c) for c in currencies.split(",") if c.strip()))
    else:
        codes = [FX_BASE, *fx_rates.currencies()]
    if len(codes) < 2:
        raise HTTPException(status_code=400, detail="Give at least two currencies")
    fixes = {}
    for code in codes:
        if code == FX_BASE:
            continue
        fixes[code] = _fix_on(code, day)
        if fixes[code] is None:
            raise HTTPException(status_code=404, detail=f"No Bank of Ghana rate for {code} on or before {day}")
    ghs_per_unit = [fixes[c].mid if c in fixes else 1.0 for c in codes]

    def describe(code: str) -> dict:
        name, symbol = (FX_BASE_NAME, FX_BASE_SYMBOL) if code == FX_BASE else CURRENCIES[code][:2]
        return {"code": code, "name": name, "symbol": symbol}

    return {
        "date": day.isoformat() if day else None,
        "as_of": max(r.date for r in fixes.values()).isoformat() if fixes else None,
        "rate": "mid",
        "currencies": [describe(c) for c in codes],
        "matrix": cross_matrix(ghs_per_unit),
        "fixes": {c: r.date.isoformat() for c, r in fixes.items()},
        "source": FX_SOURCE,
        "source_url": FX_SOURCE_URL,
    }


@app.get("/fx/forwards")
async def fx_forwards():
    """Indicative synthetic forwards for USD, EUR and GBP against the cedi,
    by covered interest parity: the BoG mid as spot, the latest T-bill
    auction yields for the cedi leg (interpolated by tenor, ACT/364) and
    SOFR, €STR or SONIA held flat for the foreign leg. The inputs are in
    the reply, each with its date. A currency missing an input is listed
    in `unavailable` with the reason. 503 until both a BoG fix and the
    T-bill yields are held. See app/fx/forwards.py."""
    _require_fx()
    bills = [r for r in (auctions.latest(s) for s in BILL_TENORS) if r is not None]
    if not bills:
        raise HTTPException(
            status_code=503,
            detail={"message": "No T-bill auction yields yet for the cedi leg; the first pull may still be running",
                    **auctions.describe()},
        )
    ghs_curve = [(r.tenor_days, r.interest_rate) for r in bills]
    pairs, unavailable = [], {}
    for code, bench in OVERNIGHT_BENCHMARKS.items():
        spot = fx_rates.latest(code)
        foreign = overnight_rates.latest(code)
        if spot is None:
            unavailable[code] = f"No Bank of Ghana rate for {code} yet"
            continue
        if foreign is None:
            unavailable[code] = f"No {bench.name} rate yet"
            continue
        curve = forward_curve(spot.mid, spot.date, ghs_curve, foreign.rate, bench.basis)
        pairs.append({
            "pair": f"{code}{FX_BASE}",
            "currency": code,
            "name": CURRENCIES[code][0],
            "spot": spot.mid,
            "spot_date": spot.date.isoformat(),
            "foreign_rate": {
                "benchmark": bench.name, "rate": foreign.rate, "date": foreign.date.isoformat(),
                "day_count": f"ACT/{bench.basis}", "publisher": bench.publisher, "url": bench.url,
            },
            "tenors": [{
                "tenor": p.tenor, "value_date": p.value_date.isoformat(), "days": p.days,
                "ghs_rate": p.ghs_rate, "foreign_rate": p.foreign_rate, "outright": p.outright,
                "points": p.points, "premium_pa": p.premium_pa,
            } for p in curve],
        })
    return {
        "method": "covered interest parity",
        "rate": "mid",
        "cedi_curve": {
            "source": AUCTION_SOURCE,
            "source_url": AUCTION_SOURCE_URL,
            "day_count": f"ACT/{GHS_BASIS}",
            "points": [{"security": r.security, "tenor_days": r.tenor_days, "rate": r.interest_rate,
                        "issue_date": r.issue_date.isoformat(), "tender": r.tender} for r in bills],
        },
        "spot_source": FX_SOURCE,
        "pairs": pairs,
        "unavailable": unavailable,
        "status": overnight_rates.describe(),
    }


@app.get("/fx", response_class=HTMLResponse)
async def fx_page():
    """The FX tab: BoG reference rates, the cross matrix and synthetic forwards."""
    return (STATIC_DIR / "fx.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- /commodities

def _commodity_code(code: str) -> str:
    """A commodity we offer, upper-cased. 400 if we don't offer it; 503
    if we do but hold no prices for it yet."""
    code = code.strip().upper()
    if code not in COMMODITIES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown commodity '{code}'. Expected one of: {', '.join(COMMODITIES)}",
        )
    if not commodity_prices.has(code):
        raise HTTPException(status_code=503, detail=f"No prices for {code} yet; try again shortly")
    return code


def _require_commodities() -> None:
    """503 until the first pull has landed."""
    if not commodity_prices.commodities():
        raise HTTPException(
            status_code=503,
            detail={"message": "No commodity prices yet; the first pull may still be running",
                    **commodity_prices.describe()},
        )


def _month_param(text: Optional[str], name: str) -> Optional[date]:
    """A YYYY-MM query parameter as its month's first day. 400 if malformed."""
    if text is None:
        return None
    try:
        return parse_month(text)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM, got '{text}'")


def _in_cedis(usd: float, fx: dict[date, tuple[float, int]], month: date) -> Optional[float]:
    """`usd` (a month's average) in cedis at that month's average BoG
    USD mid, or None if no fixes are held for the month."""
    rate = fx.get(month)
    return None if rate is None else usd * rate[0]


def _percent(now: float, then: Optional[float]) -> Optional[float]:
    return None if then is None or then <= 0 else (now / then - 1) * 100


@app.get("/commodities/prices")
async def get_commodity_prices(month: Optional[str] = Query(None, description="YYYY-MM; default the latest")):
    """Monthly average prices of the commodities offered: in US dollars
    per unit as published (`usd`, `unit`), and in cedis at that month's
    average Bank of Ghana USD mid (`ghs`; null for a month with no fixes
    held). The latest month by default; `month` gives the one in force
    then (a series that skipped it shows its month before). Each row has
    the move on the month before (`change_percent`) and on the year
    before (`yoy_percent`, and `ghs_yoy_percent` in cedis, which includes
    the cedi's own move). 400 for a malformed month, 503 until the first
    pull has landed, 404 for a month before the history. See
    app/commodities/."""
    _require_commodities()
    in_force = _month_param(month, "month")
    picked = []
    for code in commodity_prices.commodities():
        p = commodity_prices.latest(code) if in_force is None else commodity_prices.on(code, in_force)
        if p is not None:
            picked.append((code, p))
    if not picked:
        raise HTTPException(status_code=404, detail=f"No commodity prices held for {month}")
    earliest = min(p.month for _, p in picked)
    fx = monthly_means(fx_rates.history("USD", add_months(earliest, -12))) if fx_rates.has("USD") else {}

    rows = []
    for code, p in picked:
        c = COMMODITIES[code]
        prev = commodity_prices.on(code, add_months(p.month, -1))
        year_ago = commodity_prices.on(code, add_months(p.month, -12))
        ghs =_in_cedis(p.value, fx, p.month)
        ghs_year_ago = None if year_ago is None else _in_cedis(year_ago.value, fx, year_ago.month)
        rate = fx.get(p.month)
        rows.append({
            "code": code,
            "name": c.name,
            "group": c.group,
            "per": c.per,
            "unit": p.unit,
            "source": p.source,
            "series": p.series,
            "month": month_key(p.month),
            "usd": p.value,
            "previous_month": month_key(prev.month) if prev else None,
            "previous_usd": prev.value if prev else None,
            "change_percent": _percent(p.value, prev.value if prev else None),
            "year_ago_month": month_key(year_ago.month) if year_ago else None,
            "year_ago_usd": year_ago.value if year_ago else None,
            "yoy_percent": _percent(p.value, year_ago.value if year_ago else None),
            "ghs": ghs,
            "ghs_per_usd": rate[0] if rate else None,
            "fx_fixes": rate[1] if rate else 0,
            "ghs_yoy_percent": None if ghs is None else _percent(ghs, ghs_year_ago),
        })
    return {
        "currency": "USD",
        "base": FX_BASE,
        "month": month_key(in_force) if in_force else None,
        "as_of": max(r["month"] for r in rows),
        "commodities": rows,
        "sources": {k: {"name": s.name, "url": s.url} for k, s in COMMODITY_SOURCES.items()},
        "fx_source": FX_SOURCE,
        "status": commodity_prices.describe(),
    }


@app.get("/commodities/{code}/history")
async def get_commodity_history(code: str, since: Optional[str] = Query(None, description="YYYY-MM; default all")):
    """One commodity's monthly averages from `since` (all if omitted),
    oldest first: `usd` as published and `ghs` at that month's average
    Bank of Ghana USD mid (null before the FX history held). 400 for a
    commodity we don't offer or a malformed month, 503 before its first
    pull."""
    code = _commodity_code(code)
    start = _month_param(since, "since")
    prices = commodity_prices.history(code, start)
    fx = monthly_means(fx_rates.history("USD", prices[0].month)) if prices and fx_rates.has("USD") else {}
    c = COMMODITIES[code]
    latest: Optional[Price] = commodity_prices.latest(code)
    return {
        "code": code,
        "name": c.name,
        "per": c.per,
        "unit": latest.unit if latest else c.unit,
        "source": latest.source if latest else None,
        "series": latest.series if latest else None,
        "points": [
            {"month": month_key(p.month), "usd": p.value, "ghs": _in_cedis(p.value, fx, p.month)}
            for p in prices
        ],
    }


@app.get("/commodities", response_class=HTMLResponse)
async def commodities_page():
    """The Commodities tab: monthly reference prices in US$ and cedis."""
    return (STATIC_DIR / "commodities.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------- calculators

class FxOptionRequest(BaseModel):
    """POST /calc/fx-option. Only the terms are needed; every market input
    left out is filled from the data the service holds (and the reply says
    where each came from)."""
    model_config = ConfigDict(extra="forbid")

    currency: str = "USD"
    kind: Literal["call", "put"] = "call"          # call: the right to buy the currency; put: to sell it
    notional: float = Field(100_000, gt=0)         # in the foreign currency
    tenor: Optional[str] = None                    # 1W 1M 2M 3M 6M 9M 1Y; ignored if expiry is given
    expiry: Optional[date] = None
    strike: Optional[float] = Field(None, gt=0)    # cedis per unit; default the forward to expiry
    valuation_date: Optional[date] = None          # default today
    # Overrides, % a year where a rate:
    spot: Optional[float] = Field(None, gt=0)
    vol: Optional[float] = Field(None, gt=0, le=500)
    ghs_rate: Optional[float] = Field(None, ge=-5, le=200)
    foreign_rate: Optional[float] = Field(None, ge=-5, le=200)
    # A price someone quoted, in cedis for the whole notional: the reply
    # adds the volatility it implies.
    quoted_premium: Optional[float] = Field(None, gt=0)


def _bill_curve(day: date) -> list[AuctionResult]:
    """The latest 91, 182 and 364-day auction issued on or before `day`."""
    out = []
    for security in BILL_TENORS:
        held = [r for r in auctions.history(security) if r.issue_date <= day]
        if held:
            out.append(held[-1])
    return out


def _input(key: str, label: str, value: float, unit: str, source: str, as_of: Optional[date], note: str) -> dict:
    """One market input as the reply reports it. source: "market" (data
    the service holds), "estimate" (calculated from it, not a quote) or
    "you" (an override)."""
    return {"key": key, "label": label, "value": value, "unit": unit, "source": source,
            "as_of": as_of.isoformat() if as_of else None, "note": note}


@app.post("/calc/fx-option")
async def calc_fx_option(req: FxOptionRequest):
    """A European option on a currency against the cedi, priced with
    QuantLib (Garman-Kohlhagen): the premium, the Greeks, the forward to
    compare with, the break-even, profit and loss across spot (today and
    at expiry), and, given a quoted price, the volatility it implies.
    Market inputs not given are filled in: spot from the BoG fix, the cedi
    rate from the T-bill auctions, the foreign rate from SOFR/€STR/SONIA
    and volatility from the fix's own history. Each input is reported with
    its source and date. 400 for terms that can't be priced or an input
    the service can't fill. See app/derivatives/fx_option.py."""
    code = req.currency.strip().upper()
    if code == FX_BASE or code not in CURRENCIES:
        raise HTTPException(status_code=400, detail=f"Unknown currency '{code}'. Expected one of: {', '.join(CURRENCIES)}")
    today = req.valuation_date or calendar.local_date(datetime.now(timezone.utc))
    if req.expiry is not None:
        expiry = req.expiry
    else:
        tenor = (req.tenor or "3M").upper()
        match = next((t for t in FORWARD_TENORS if t[0] == tenor), None)
        if match is None:
            raise HTTPException(status_code=400, detail=f"Unknown tenor '{tenor}'. Expected one of: "
                                                        f"{', '.join(t[0] for t in FORWARD_TENORS)}")
        expiry = add_months(today, match[1]) + timedelta(days=match[2])
    days = (expiry - today).days
    if days <= 0:
        raise HTTPException(status_code=400, detail="The expiry must be after the valuation date")
    name, symbol, _ = CURRENCIES[code]
    inputs = []

    # Spot: the BoG mid in force on the valuation date.
    fix = fx_rates.on(code, today) if fx_rates.has(code) else None
    if req.spot is not None:
        spot = req.spot
        inputs.append(_input("spot", f"Spot {code}/GHS", spot, "GH₵ per unit", "you", None, "Entered"))
    elif fix is None:
        raise HTTPException(status_code=400, detail=f"No Bank of Ghana rate for {code} yet; enter the spot rate")
    else:
        spot = fix.mid
        inputs.append(_input("spot", f"Spot {code}/GHS", spot, "GH₵ per unit", "market", fix.date,
                             "Bank of Ghana interbank mid"))

    # Cedi rate: the T-bill curve at the option's life.
    if req.ghs_rate is not None:
        ghs_rate = req.ghs_rate
        inputs.append(_input("ghs_rate", "Cedi interest rate", ghs_rate, "% a year, ACT/364", "you", None, "Entered"))
    else:
        bills = _bill_curve(today)
        if not bills:
            raise HTTPException(status_code=400, detail="No T-bill auction yields held; enter the cedi interest rate")
        ghs_rate = interpolate([(r.tenor_days, r.interest_rate) for r in bills], days)
        inputs.append(_input(
            "ghs_rate", "Cedi interest rate", ghs_rate, "% a year, ACT/364", "market",
            max(r.issue_date for r in bills),
            f"T-bill auction yields ({', '.join(f'{r.tenor_days}d {r.interest_rate:.2f}%' for r in bills)}) "
            f"read at {days} days"))

    # Foreign rate: the overnight benchmark, held flat.
    bench = OVERNIGHT_BENCHMARKS.get(code)
    basis = bench.basis if bench else 365
    if req.foreign_rate is not None:
        foreign_rate = req.foreign_rate
        inputs.append(_input("foreign_rate", f"{code} interest rate", foreign_rate, f"% a year, ACT/{basis}", "you",
                             None, "Entered"))
    else:
        overnight = overnight_rates.on(code, today) if bench else None
        if overnight is None:
            raise HTTPException(status_code=400, detail=f"No {code} interest rate held; enter the {code} interest rate")
        foreign_rate = overnight.rate
        inputs.append(_input("foreign_rate", f"{code} interest rate", foreign_rate, f"% a year, ACT/{basis}", "market",
                             overnight.date, f"{bench.name} ({bench.publisher}), overnight, held flat to expiry"))

    # Volatility: how much the fix has moved, over a window matching the option's life.
    mids = [r.mid for r in fx_rates.history(code) if r.date <= today]
    hist = {label: historical_vol(mids, n) for label, n in VOL_WINDOWS.items()}
    window = vol_window_for(days)
    if req.vol is not None:
        vol = req.vol
        inputs.append(_input("vol", "Volatility", vol, "% a year", "you", None, "Entered"))
    else:
        vol = hist[window] or next((v for v in hist.values() if v), None)
        if not vol:
            raise HTTPException(status_code=400, detail=f"Not enough {code} fix history for a volatility; enter one")
        used = window if hist[window] else next(k for k, v in hist.items() if v)
        inputs.append(_input("vol", "Volatility", vol, "% a year", "estimate", fix.date if fix else None,
                             f"Historical: how much the BoG fix moved over the last {used} "
                             "(no traded cedi option volatility exists)"))

    market = OptionMarket(spot=spot, vol=vol, ghs_rate=ghs_rate, foreign_rate=foreign_rate, foreign_basis=basis)
    fwd = fx_forward(today, expiry, market)
    strike = req.strike if req.strike is not None else round(fwd, 4)
    try:
        v = option_value(req.kind, strike, today, expiry, market)
    except OptionError as e:
        raise HTTPException(status_code=400, detail=str(e))

    implied = None
    if req.quoted_premium is not None:
        try:
            implied = {"premium": req.quoted_premium,
                       "vol": option_implied_vol(req.kind, strike, today, expiry, market, req.quoted_premium / req.notional),
                       "error": None}
        except OptionError as e:
            implied = {"premium": req.quoted_premium, "vol": None, "error": str(e)}

    # Profit and loss across spot, net of the premium paid: if spot moved
    # today, and at expiry.
    n = req.notional
    width = max(0.05, 3 * vol / 100 * math.sqrt(days / 365))
    lo, hi = min(spot, strike) * (1 - width), max(spot, strike) * (1 + width)
    spots = [lo + (hi - lo) * i / 60 for i in range(61)]
    today_values = option_values_across(req.kind, strike, today, expiry, market, spots)
    payoff = (lambda s: max(s - strike, 0.0)) if req.kind == "call" else (lambda s: max(strike - s, 0.0))
    premium_total = v.premium * n
    breakeven = strike + v.premium if req.kind == "call" else strike - v.premium

    return {
        "pair": f"{code}{FX_BASE}",
        "currency": {"code": code, "name": name, "symbol": symbol},
        "kind": req.kind,
        "notional": n,
        "strike": strike,
        "strike_source": "you" if req.strike is not None else "forward",
        "valuation_date": today.isoformat(),
        "expiry": expiry.isoformat(),
        "days": days,
        "inputs": inputs,
        "historical_vol": hist,
        "forward": fwd,
        "premium": {
            "total": premium_total,
            "per_unit": v.premium,
            "pips": v.premium / 1e-4,
            "percent_of_notional": v.premium / spot * 100,
        },
        "breakeven": breakeven,
        # The middle 90% of where the rate may be at expiry, under the same
        # assumptions as the premium: what doing nothing could cost.
        "likely_range": list(likely_range(fwd, vol, days)),
        # Per unit as QuantLib gives them, and scaled to the whole notional, in cedis.
        "greeks": {
            "delta": v.delta, "gamma": v.gamma, "vega": v.vega, "theta": v.theta,
            "rho_ghs": v.rho_ghs, "rho_foreign": v.rho_foreign,
            "delta_amount": v.delta * n,          # units of the currency the option behaves like
            "vega_total": v.vega * n,             # cedis per 1 point of volatility
            "theta_total": v.theta * n,           # cedis per day
            "rho_ghs_total": v.rho_ghs * n,       # cedis per 1 point of cedi rate
            "rho_foreign_total": v.rho_foreign * n,
        },
        "implied_vol": implied,
        "profit_loss": {
            "spots": spots,
            "today": [(x - v.premium) * n for x in today_values],
            "at_expiry": [(payoff(s) - v.premium) * n for s in spots],
        },
        "model": "Garman-Kohlhagen (European), QuantLib " + ql_version,
    }


@app.get("/hedging", response_class=HTMLResponse)
async def hedging_page():
    """The Hedging tab: a foreign-currency payment or receipt, and what
    doing nothing, a forward and an option would each cost, by rate.
    Prices through POST /calc/fx-option."""
    return (STATIC_DIR / "hedging.html").read_text(encoding="utf-8")


@app.get("/calculators")
async def calculators_page():
    """The FX option calculator's old address: the Hedging page replaced it."""
    return RedirectResponse("/hedging", status_code=301)


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


@app.get("/spreads", response_model=SpreadAnalysis)
async def spreads(day: Optional[date] = Query(None, alias="date")):
    """Spread analysis for a session (app/analytics/spreads.py): a
    Nelson-Siegel curve fitted to the GoG closes, each bill and bond's
    residual to it (rich/cheap, bp), each corporate's yield from its
    price and spread over it, and spreads by issuer. `date` as for
    /curve; 400 for a date after the current session."""
    try:
        curve = fixed_income.curve(day)
        _, corporates = fixed_income.corporate_closes(day)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return spread_analysis(curve, corporates)


def _require_auctions() -> None:
    if auctions.as_of is None:
        raise HTTPException(
            status_code=503,
            detail={"message": "No Bank of Ghana auction results yet; the first pull may still be running",
                    **auctions.describe()},
        )


@app.get("/auctions/monitor", response_model=AuctionMonitor)
async def auction_monitor_view():
    """The new-issue monitor (app/auctions/monitor.py): the latest T-bill
    auction's rates and moves, recent new bills against their GFIM close,
    the next tender (estimated) and the bills it rolls over, and the
    latest note or bond auction. 503 until the first pull from BoG lands."""
    _require_auctions()
    report = fixed_income.report()
    today = fixed_income_calendar.local_date(datetime.now(timezone.utc))
    return auction_monitor(auctions, report.treasury_bill, report.report_date, today, fixed_income_calendar,
                           AUCTION_SOURCE, AUCTION_SOURCE_URL)


@app.get("/auctions/results", response_model=list[AuctionResultOut])
async def auction_results(
    security: Optional[str] = None,
    since: Optional[date] = None,
    limit: int = Query(500, ge=1, le=5000),
):
    """Auction results as BoG published them, newest first: every
    security, or one (`security`, as BoG names it: "91 DAY BILL",
    "2 YR FXR NOTE"; case and spacing don't matter), issued on or after
    `since`, at most `limit`. Each carries its change from the same
    security's previous auction. 400 for a security never auctioned;
    503 until the first pull lands."""
    _require_auctions()
    if security is not None:
        security = " ".join(security.upper().split())
        if security not in auctions.securities():
            raise HTTPException(
                status_code=400,
                detail=f"Unknown security '{security}'. Expected one of: {', '.join(auctions.securities())}",
            )
        results = list(reversed(auctions.history(security, since)))
    else:
        results = auctions.results(since)
    return [auction_result_out(r, auctions.previous(r)) for r in results[:limit]]


ECON_CATEGORIES = ("monetary_policy", "prices", "growth", "trade", "labour", "statistics", "auction", "holiday")
ECON_MAX_DAYS = 400


@app.get("/economic-calendar", response_model=EconCalendar)
async def economic_calendar(
    start: Optional[date] = None,
    end: Optional[date] = None,
    category: Optional[str] = None,
    importance: str = Query("low", pattern="^(high|medium|low)$"),
):
    """The economic calendar (app/econ/events.py): BoG policy rate
    decisions (past from BoG's history, coming from the MPC meeting
    dates), GSS releases (to the week), T-bill auctions and market
    holidays, for [start, end] (default: a week back to 60 days on).
    `category` is a comma-separated list of monetary_policy, prices,
    growth, trade, labour, statistics, auction, holiday; `importance`
    the lowest to include (low = all). Also the policy rate in force and
    each source's last pull. 400 for an end before the start, a range
    over 400 days, or an unknown category."""
    today = fixed_income_calendar.local_date(datetime.now(timezone.utc))
    start = start or today - timedelta(days=7)
    end = end or today + timedelta(days=60)
    if end < start or (end - start).days > ECON_MAX_DAYS:
        raise HTTPException(status_code=400, detail=f"start..end must run forwards, at most {ECON_MAX_DAYS} days")
    wanted = None
    if category:
        wanted = {c.strip().lower() for c in category.split(",") if c.strip()}
        unknown = wanted - set(ECON_CATEGORIES)
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown category {', '.join(sorted(unknown))}. "
                                                        f"Expected: {', '.join(ECON_CATEGORIES)}")
    rank = {"high": 0, "medium": 1, "low": 2}
    events = calendar_events(
        start, end, today, econ.decisions, mpc_meetings, econ.releases,
        auctions.results(), auctions.previous, calendar.holidays, fixed_income_calendar,
    )
    events = [e for e in events if (wanted is None or e.category in wanted)
              and rank[e.importance] <= rank[importance]]
    return EconCalendar(
        start=start, end=end, today=today, policy_rate=policy_rate_now(econ.decisions, today), events=events,
        sources={**econ.describe(), "auctions": auctions.describe(),
                 "mpc_meetings": {"through": mpc_meetings[-1].announcement.isoformat() if mpc_meetings else None}},
    )


@app.get("/economic-calendar/policy-rate", response_model=list[PolicyRatePoint])
async def policy_rate_history(since: Optional[date] = None):
    """Every BoG policy rate decision on or after `since` (all if not
    given), oldest first, with the move in bp. Empty until the first pull
    from BoG lands."""
    out, prev = [], None
    for d in econ.decisions:
        if since is None or d.date >= since:
            out.append(PolicyRatePoint(date=d.date, rate=d.rate,
                                       change_bp=None if prev is None else round((d.rate - prev) * 100, 1)))
        prev = d.rate
    return out


@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page():
    """The Calendar tab: the economic calendar and the policy rate."""
    return (STATIC_DIR / "calendar.html").read_text(encoding="utf-8")


@app.get("/auctions", response_class=HTMLResponse)
async def auctions_page():
    """The Auctions tab: the new-issue monitor and auction rate history."""
    return (STATIC_DIR / "auctions.html").read_text(encoding="utf-8")


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
    currency: Optional[str] = None,
    warmup: int = Query(0, ge=0, le=MAX_WARMUP),
):
    """OHLCV candles as JSON for charting. `range` is one of the chart
    selector ranges (1D, 5D, 1M, 6M, YTD, 1Y, 5Y, Max) and picks both the
    lookback and a suitable candle interval; `interval` overrides the
    latter. The newest candle is the live, still-open one. For bills and
    bonds OHLC is clean price and `yield` the same window in yield (null
    for equities and price-only corporates), so a chart can toggle.
    `possibly_incomplete` is true where the live feed lost a tick in
    that window under load.

    `warmup` asks for up to that many candles before the range as well,
    in their own `warmup` list (oldest first, same shape): what a moving
    average or RSI needs to have a value from the chart's first candle.
    Fewer come back where history runs out, none for Max.

    `currency` (an equity only) gives prices in that currency at the
    Bank of Ghana mid (`fx` says how): daily and weekly candles at the
    fix in force on their day (a week's last day), so a long chart
    shows what the shares were worth in that currency at the time;
    intraday candles all at the latest fix, as BoG fixes once a day.
    400 for bills and bonds, quoted per 100 of face value.
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

    equity = asset_class(symbol) == "equity"
    if currency is not None:
        if not equity:
            raise HTTPException(
                status_code=400,
                detail=f"'{symbol}' is quoted per 100 of face value, so it isn't converted; currency is for equities",
            )
        currency = _fx_code(currency)

    now = datetime.now(timezone.utc)
    start = spec.start(now)
    candles = await aggregator.get_candles(symbol, interval, start)
    earlier = []
    if warmup and start is not None:
        earlier = await aggregator.get_candles_before(symbol, interval, start, warmup)
    to_currency, fx = _candle_converter(currency, interval, now.date())

    def as_json(c: Candle) -> dict:
        return {
            "time": c.window_start.isoformat(),
            **{k: getattr(c, k) * to_currency(c) for k in ("open", "high", "low", "close")},
            "volume": c.volume,
            "yield": None if c.yield_close is None else {
                "open": c.yield_open,
                "high": c.yield_high,
                "low": c.yield_low,
                "close": c.yield_close,
            },
            "possibly_incomplete": c.possibly_incomplete,
        }

    return {
        "symbol": symbol,
        "range": range_,
        "interval": interval,
        "start": start.isoformat() if start else None,
        # What OHLC is in: GHS unless converted; null for bills and bonds
        # (per 100 of face value).
        "currency": (currency or FX_BASE) if equity else None,
        "fx": fx,
        "warmup": [as_json(c) for c in earlier],
        "candles": [as_json(c) for c in candles],
    }


def _candle_converter(currency: Optional[str], interval: str, today: date):
    """(factor(candle), fx) for /candles: what to multiply a candle's
    cedi prices by for `currency`, and how that was done. Daily and
    weekly candles take the fix in force on their (last) day; anything
    shorter takes the latest fix: BoG fixes once a business day, so
    converting each intraday candle at its own day's fix would only draw
    steps at midnight that are no price move."""
    if currency is None or currency == FX_BASE:
        return (lambda c: 1.0), None
    daily = INTERVALS[interval] >= INTERVALS["1d"]
    if daily:
        def factor(c: Candle) -> float:
            last_day = (c.window_start + INTERVALS[interval] - timedelta(days=1)).date()
            return fx_rates.per_ghs(currency, min(last_day, today))
    else:
        latest = fx_rates.per_ghs(currency)
        def factor(c: Candle) -> float:
            return latest
    return factor, {
        "currency": currency,
        "source": FX_SOURCE,
        "rate": "mid",
        "method": "daily" if daily else "latest",
        "as_of": fx_rates.latest(currency).date.isoformat(),
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


@app.get("/graph", response_class=HTMLResponse)
async def graph_page():
    """The Graph tab (Bloomberg's G and CORR): up to eight series rebased
    to 100 on one chart, from /graph/series, and their correlation
    matrix from /graph/correlation. The series and range are in the URL
    (?series=MTNGH,GSE-CI&range=1Y), so a graph can be shared."""
    return (STATIC_DIR / "graph.html").read_text(encoding="utf-8")


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


@app.get("/static/description.js")
async def description_script():
    """The stock page's Description view (#64)."""
    return FileResponse(STATIC_DIR / "description.js", media_type="text/javascript")


@app.get("/static/seasonality.js")
async def seasonality_script():
    """The stock page's Seasonality view, over /seasonality/{symbol}."""
    return FileResponse(STATIC_DIR / "seasonality.js", media_type="text/javascript")


@app.get("/static/indicators.js")
async def indicators_script():
    """Moving averages, Bollinger bands, RSI and MACD for the stock chart."""
    return FileResponse(STATIC_DIR / "indicators.js", media_type="text/javascript")


@app.get("/static/render.js")
async def render_script():
    """Frame-batched rendering helpers shared by the live pages (#22)."""
    return FileResponse(STATIC_DIR / "render.js", media_type="text/javascript")


@app.get("/static/currency.js")
async def currency_script():
    """The display-currency picker and price formatting shared by
    /ticker, /stock/{symbol} and the search bar, over /fx/rates."""
    return FileResponse(STATIC_DIR / "currency.js", media_type="text/javascript")


@app.get("/static/strip.js")
async def strip_script():
    """The market strip under the header of /ticker and the fixed income
    pages: index, FX and bill yields in place over a scrolling crawl."""
    return FileResponse(STATIC_DIR / "strip.js", media_type="text/javascript")


@app.get("/static/drawings.js")
async def drawings_script():
    """The stock chart's drawing tools (trend lines, levels, Fibonacci),
    saved through /drawings/me/{symbol}."""
    return FileResponse(STATIC_DIR / "drawings.js", media_type="text/javascript")


@app.get("/stock")
async def stock_page_default():
    return RedirectResponse(url=f"/stock/{equities.symbols[0]}")


@app.get("/stock/{symbol}", response_class=HTMLResponse)
async def stock_page(symbol: str):
    """Single-stock detail page: live quote over /ws/market plus a
    range-selectable chart from /candles, and the Description view
    (#description). The page reads the symbol from its own URL. Served
    for every listed equity, including suspended ones the feed doesn't
    carry, so their description is reachable."""
    symbol = symbol.upper()
    instrument = INSTRUMENTS.get(symbol)
    listed = instrument is not None and instrument.asset_class == "equity"
    if symbol not in equities.symbols and not listed:  # bills and bonds get their own page (#39)
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol}'")
    return (STATIC_DIR / "stock.html").read_text(encoding="utf-8")


@app.websocket("/ws/market")
async def market_websocket(websocket: WebSocket):
    """Live quotes for the symbols a client subscribes to. See
    app/gateways/websocket_gateway.py for the protocol."""
    await gateway.serve(websocket)


@app.websocket("/ws/notifications")
async def notifications_websocket(websocket: WebSocket):
    """The connected user's ticket notifications, live. The user is the
    X-User-Id header or the identity cookie; closed with 1008 if there is
    neither. See app/gateways/notification_gateway.py for the protocol."""
    user_id = websocket_user_id(websocket)
    if user_id is None:
        await websocket.close(code=1008, reason="No user: send X-User-Id or the identity cookie")
        return
    unread = len(await asyncio.to_thread(ticket_desk.store.notifications, user_id, True))
    await notification_gateway.serve(websocket, user_id, unread=unread)


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


async def fx_loop() -> None:
    """Keep the Bank of Ghana rates current: pull now (the history too,
    the first time), then every FX_REFRESH_SECONDS, or after
    FX_RETRY_SECONDS when a pull fails. Rates already stored stay in use
    meanwhile, so a BoG outage only makes them older."""
    while True:
        try:
            ok = await fx_rates.refresh()
        except Exception:
            logger.exception("Bank of Ghana FX refresh crashed")
            ok = False
        await asyncio.sleep(config.FX_REFRESH_SECONDS if ok else min(FX_RETRY_SECONDS, config.FX_REFRESH_SECONDS))


async def overnight_loop() -> None:
    """Keep SOFR, €STR and SONIA current, as fx_loop does the BoG fixes:
    pull now, then every OVERNIGHT_REFRESH_SECONDS, or after
    OVERNIGHT_RETRY_SECONDS when a pull fails."""
    while True:
        try:
            ok = await overnight_rates.refresh()
        except Exception:
            logger.exception("Overnight rates refresh crashed")
            ok = False
        await asyncio.sleep(
            config.OVERNIGHT_REFRESH_SECONDS if ok else min(OVERNIGHT_RETRY_SECONDS, config.OVERNIGHT_REFRESH_SECONDS))


async def auction_loop() -> None:
    """Keep the Bank of Ghana auction results current, as fx_loop does
    the FX fixes: pull now, then every AUCTION_REFRESH_SECONDS, or after
    AUCTION_RETRY_SECONDS when a pull fails."""
    while True:
        try:
            ok = await auctions.refresh()
        except Exception:
            logger.exception("Bank of Ghana auction refresh crashed")
            ok = False
        await asyncio.sleep(
            config.AUCTION_REFRESH_SECONDS if ok else min(AUCTION_RETRY_SECONDS, config.AUCTION_REFRESH_SECONDS))


async def econ_loop() -> None:
    """Keep the economic calendar's pulled sources current, as fx_loop
    does the FX fixes."""
    while True:
        try:
            ok = await econ.refresh()
        except Exception:
            logger.exception("Economic calendar refresh crashed")
            ok = False
        await asyncio.sleep(config.ECON_REFRESH_SECONDS if ok else min(ECON_RETRY_SECONDS, config.ECON_REFRESH_SECONDS))


async def commodity_loop() -> None:
    """Keep the commodity prices current, as fx_loop does the FX fixes:
    pull now, then every COMMODITY_REFRESH_SECONDS, or after
    COMMODITY_RETRY_SECONDS when a pull fails."""
    while True:
        try:
            ok = await commodity_prices.refresh()
        except Exception:
            logger.exception("Commodity prices refresh crashed")
            ok = False
        await asyncio.sleep(
            config.COMMODITY_REFRESH_SECONDS if ok else min(COMMODITY_RETRY_SECONDS, config.COMMODITY_REFRESH_SECONDS))


@app.on_event("startup")
async def startup():
    global consumer_task, status_task, fx_task, overnight_task, auction_task, econ_task, commodity_task
    await asyncio.to_thread(instrument_store.seed, INSTRUMENTS.values())
    await asyncio.to_thread(company_store.seed, COMPANIES.values())
    # Rates stored by earlier runs are usable at once; the pull for
    # newer ones runs in the background, never holding up startup.
    await asyncio.to_thread(fx_rates.reload)
    if config.FX_REFRESH_SECONDS > 0:
        fx_task = asyncio.create_task(fx_loop())
    await asyncio.to_thread(overnight_rates.reload)
    if config.OVERNIGHT_REFRESH_SECONDS > 0:
        overnight_task = asyncio.create_task(overnight_loop())
    await asyncio.to_thread(auctions.reload)
    if config.AUCTION_REFRESH_SECONDS > 0:
        auction_task = asyncio.create_task(auction_loop())
    await asyncio.to_thread(econ.reload)
    if config.ECON_REFRESH_SECONDS > 0:
        econ_task = asyncio.create_task(econ_loop())
    await asyncio.to_thread(commodity_prices.reload)
    if config.COMMODITY_REFRESH_SECONDS > 0:
        commodity_task = asyncio.create_task(commodity_loop())
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
    fx_rates.close()  # a pull in progress stops at its next page
    auctions.close()
    for task in (fx_task, overnight_task, auction_task, econ_task, commodity_task, status_task, consumer_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await aggregator.stop()
    await buffer.stop()
    await connector.disconnect()
