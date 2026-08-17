import asyncio

from fastapi import FastAPI, WebSocket

from app import config
from app.connectors.market_connector import MockMarketConnector
from app.gateways.websocket_gateway import WebSocketGateway
from app.models.market_data import MarketData
from app.processors.market_processor import MarketProcessor

app = FastAPI(title="Market Data Integration Service")

connector = MockMarketConnector(
    symbols=config.SYMBOLS,
    interval_seconds=config.MOCK_INTERVAL_SECONDS,
)

processor = MarketProcessor()
gateway = WebSocketGateway()

# The queue decouples ingestion (connector) from processing/delivery
# (processor + gateway). See processors/market_processor.py for why.
queue: "asyncio.Queue[MarketData]" = asyncio.Queue(maxsize=config.QUEUE_MAX_SIZE)


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/market/{symbol}")
async def get_market(symbol: str):
    data = processor.get_latest(symbol.upper())
    if data is None:
        return {"error": "Symbol not found"}
    return data


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

    except Exception:
        await gateway.disconnect(websocket)


async def producer_loop() -> None:
    """Connector -> queue. Never blocks on processing or delivery."""
    await connector.connect()

    async for data in connector.stream():
        await queue.put(data)


async def consumer_loop() -> None:
    """Queue -> processor -> gateway broadcast."""
    await processor.consume(queue, on_processed=gateway.broadcast)


@app.on_event("startup")
async def startup():
    asyncio.create_task(producer_loop())
    asyncio.create_task(consumer_loop())


@app.on_event("shutdown")
async def shutdown():
    await connector.disconnect()
