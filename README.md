# Market Integration Service

A market-data integration service that ingests a live price feed, normalizes
it into a canonical format, and exposes it for consumption (initially via
REST/WebSocket, eventually via Symphony webhooks).

Currently wired to a **mock** provider that simulates live ticks for a
configurable list of symbols, so the full pipeline — connector → queue →
processor → gateway — can be built and tested before a real market-data
source is chosen.

## Architecture

```text
Market Data Provider (mock, later real)
            │
            ▼
     MarketConnector            (app/connectors/)
            │  MarketData
            ▼
     asyncio.Queue              (decouples ingestion from processing)
            │
            ▼
     MarketProcessor            (app/processors/)
            │  validates, normalizes, tracks latest state per symbol
            ▼
   ┌────────┴─────────┐
   ▼                  ▼
REST endpoint    WebSocketGateway   (app/gateways/)
(/market/{symbol})     │
                        ▼
                  Connected clients (eventually Symphony)
```

- **`app/connectors/`** — talks to the market-data source. `base_connector.py`
  defines the interface every provider adapter must implement
  (`connect`, `stream`, `disconnect`, `normalize`). `market_connector.py` is
  the current mock implementation. A real provider gets its own class here
  implementing the same interface — nothing else in the app changes.
- **`app/models/`** — `MarketData`, the canonical shape everything downstream
  of a connector deals with (`symbol`, `price`, `volume`, `timestamp`).
- **`app/processors/`** — consumes data off the queue, validates it, and
  keeps the latest known value per symbol.
- **`app/gateways/`** — delivery layer. Currently a WebSocket gateway that
  broadcasts to connected clients; a webhook gateway for Symphony workflow
  events would live here too.
- **`app/main.py`** — wires everything together and exposes the FastAPI app.
- **`app/config.py`** — environment-driven settings (tracked symbols, mock
  update interval, queue size, provider URL/key placeholders).

## Requirements

- Python 3.10+ (3.12 recommended)
- Windows PowerShell, macOS/Linux shell — instructions below cover both

## Setup (virtual environment)

### 1. Clone/unzip the project and move into it

```powershell
cd market-integration
```

### 2. Create a virtual environment

**Windows (PowerShell):**
```powershell
py -3.12 -m venv .venv
.venv\Scripts\activate
```

**macOS/Linux:**
```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

You should see `(.venv)` appear at the start of your prompt once it's active.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

Copy the example file and adjust as needed:

**Windows (PowerShell):**
```powershell
Copy-Item .env.example .env
```

**macOS/Linux:**
```bash
cp .env.example .env
```

Open `.env` and adjust if you want different tracked symbols:

```env
MARKET_SYMBOLS=AAPL,MSFT,TSLA
MOCK_INTERVAL_SECONDS=0.5
QUEUE_MAX_SIZE=0
```

Only symbols listed in `MARKET_SYMBOLS` will return data from
`/market/{symbol}` — unlisted symbols return `{"error": "Symbol not found"}`.

## Running the service

From the project root, with the venv activated:

```bash
uvicorn app.main:app --reload
```

You should see:

```text
Connecting to (mock) market data provider...
Connected to (mock) market data provider.
Uvicorn running on http://127.0.0.1:8000
```

## Using the service

| URL                              | Purpose                              |
|-----------------------------------|---------------------------------------|
| `http://127.0.0.1:8000/docs`      | Interactive Swagger UI — try endpoints directly in the browser |
| `http://127.0.0.1:8000/redoc`     | Alternative API documentation        |
| `http://127.0.0.1:8000/health`    | Health check                         |
| `http://127.0.0.1:8000/market/{symbol}` | Latest snapshot for a tracked symbol (e.g. `/market/AAPL`) |
| `ws://127.0.0.1:8000/ws/market`   | WebSocket — live-streaming updates, sends initial state then pushes ticks as they arrive |

`127.0.0.1` means "this machine only" — the service isn't reachable from
another computer or from Symphony yet. That's expected during development.

### Quick test with curl

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/market/AAPL
```

### Quick test of the live WebSocket feed

Save this as `test-client.html` and open it in a browser while the server
is running:

```html
<!DOCTYPE html>
<html>
<head><title>Market Feed</title></head>
<body>
  <h1>Live Market Data</h1>
  <pre id="market"></pre>
  <script>
    const socket = new WebSocket("ws://127.0.0.1:8000/ws/market");
    socket.onmessage = (event) => {
      document.getElementById("market").textContent =
        JSON.stringify(JSON.parse(event.data), null, 2);
    };
  </script>
</body>
</html>
```

## Stopping the service

Press `Ctrl+C` in the terminal running Uvicorn. The connector's
`disconnect()` runs automatically on shutdown.

## Deactivating the virtual environment

```bash
deactivate
```

## Project structure

```text
market-integration/
│
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI app, wires everything together
│   ├── config.py                  # env-driven settings
│   │
│   ├── models/
│   │   └── market_data.py         # canonical MarketData schema
│   │
│   ├── connectors/
│   │   ├── base_connector.py      # interface every provider must implement
│   │   └── market_connector.py    # mock provider (current)
│   │
│   ├── processors/
│   │   └── market_processor.py    # validation, latest-state tracking
│   │
│   └── gateways/
│       └── websocket_gateway.py   # client tracking + broadcast
│
├── requirements.txt
├── .env.example
└── README.md
```

## Replacing the mock with a real provider

1. Create a new class in `app/connectors/` (e.g. `real_market_connector.py`)
   implementing `BaseMarketConnector`: `connect()` (auth + subscribe),
   `stream()` (yield normalized `MarketData`), `disconnect()`, and
   `normalize()` (map the provider's raw fields to `MarketData`).
2. Swap the import in `app/main.py` from `MockMarketConnector` to the new
   class.
3. Add any provider-specific settings (URL, API key, symbol format) to
   `app/config.py` and `.env`.

Nothing in `processors/`, `gateways/`, or `main.py`'s wiring needs to
change — that's the point of the connector interface.

## Roadmap

- [ ] Real market-data provider connector (pending provider selection)
- [ ] Webhook/event gateway for Symphony workflow triggers (e.g. threshold
      crossings, % change alerts)
- [ ] Reconnect/backoff logic for the connector
- [ ] Bounded queue + backpressure policy for high-throughput feeds
- [ ] Observability (connection status, messages/sec, latency, dropped
      messages)
