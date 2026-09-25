import asyncio
import csv
import io
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app import config
from app.aggregation.market_aggregator import MarketAggregator
from app.aggregation.ranges import RANGES
from app.connectors.market_connector import MockMarketConnector
from app.gateways.websocket_gateway import WebSocketGateway
from app.processors.market_processor import MarketProcessor
from app.queue.market_buffer import MarketDataBuffer
from app.validation.market_validator import ValidatingStream

logger = logging.getLogger(__name__)

app = FastAPI(title="Market Data Integration Service")

STATIC_DIR = Path(__file__).parent / "static"

connector = MockMarketConnector(
    symbols=config.SYMBOLS,
    interval_seconds=config.MOCK_INTERVAL_SECONDS,
)

processor = MarketProcessor()
gateway = WebSocketGateway()

# The buffer decouples ingestion (connector) from its downstream
# consumers, each getting an independent bounded queue with drop-oldest
# backpressure. See queue/market_buffer.py for why.
buffer = MarketDataBuffer(connector.stream(), maxsize=config.QUEUE_MAX_SIZE)

# Real-time delivery: every tick, validated so a bad tick never reaches
# a WebSocket client.
validated_feed = ValidatingStream(buffer.subscribe("processor"), name="processor")

# OHLCV persistence: a separate branch off the buffer, so a slow database
# write can never delay real-time delivery. Validates independently.
aggregator = MarketAggregator(buffer.subscribe("aggregator"))

# Set once startup() creates it, so /health can check whether it's still
# alive (e.g. hasn't died from an unhandled exception in processor.consume).
consumer_task: Optional[asyncio.Task] = None


@app.get("/health")
async def health():
    components = {
        "connector": connector.running,
        "buffer": buffer.healthy,
        "processor": consumer_task is not None and not consumer_task.done(),
        "aggregator": aggregator.healthy,
    }
    healthy = all(components.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "healthy" if healthy else "unhealthy",
            "components": components,
        },
    )


@app.get("/market/{symbol}")
async def get_market(symbol: str):
    data = processor.get_latest(symbol.upper())
    if data is None:
        return {"error": "Symbol not found"}
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
    latter. The newest candle is the live, still-open one.
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
                "open, high, low, close, volume, tick_count "
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
    ])
    writer.writerows(rows)

    filename = f"market_candles_{symbol.upper()}.csv" if symbol else "market_candles.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@app.get("/ticker", response_class=HTMLResponse)
async def ticker_page():
    """Live-updating quote card UI, driven by the same /ws/market feed."""
    return (STATIC_DIR / "ticker.html").read_text(encoding="utf-8")


@app.get("/stock")
async def stock_page_default():
    return RedirectResponse(url=f"/stock/{connector.symbols[0]}")


@app.get("/stock/{symbol}", response_class=HTMLResponse)
async def stock_page(symbol: str):
    """Single-stock detail page: live quote over /ws/market plus a
    range-selectable chart from /candles. The page reads the symbol from
    its own URL."""
    if symbol.upper() not in connector.symbols:
        raise HTTPException(status_code=404, detail=f"Unknown symbol '{symbol.upper()}'")
    return (STATIC_DIR / "stock.html").read_text(encoding="utf-8")


@app.websocket("/ws/market")
async def market_websocket(websocket: WebSocket):
    await gateway.connect(websocket)

    try:
        await websocket.send_json({
            "type": "initial_state",
            "data": {
                symbol: data.model_dump(mode="json")
                for symbol, data in processor.get_all_latest().items()
            },
        })

        while True:
            # Keep the connection open; we don't expect the client to
            # send anything, but we need to await something so a client
            # disconnect raises and hits the except block below.
            await websocket.receive_text()

    except WebSocketDisconnect:
        await gateway.disconnect(websocket)
    except Exception:
        logger.exception("Market websocket connection failed")
        await gateway.disconnect(websocket)


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


@app.on_event("startup")
async def startup():
    global consumer_task
    await connector.connect()
    # Before any live ticks flow, so history is in place (and open
    # windows seeded) by the time the aggregator starts recording.
    await aggregator.backfill(connector)
    await buffer.start()
    await aggregator.start()
    consumer_task = asyncio.create_task(consumer_loop())


@app.on_event("shutdown")
async def shutdown():
    if consumer_task is not None:
        consumer_task.cancel()
        try:
            await consumer_task
        except asyncio.CancelledError:
            pass
    await aggregator.stop()
    await buffer.stop()
    await connector.disconnect()
