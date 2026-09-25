# Market Integration Service

A market-data integration service that ingests a live price feed, normalizes
it into a canonical format, and exposes it for consumption (initially via
REST/WebSocket, eventually via Symphony webhooks).

Currently wired to a **mock** provider that simulates live ticks for a
configurable list of symbols, so the full pipeline — connector → buffer →
validation/processor → gateway, plus a parallel aggregation branch — can be
built and tested before a real market-data source is chosen.

## Architecture

```text
Market Data Provider (mock, later real)
            │
            ▼
     MarketConnector                (app/connectors/)
            │  MarketData
            ▼
     MarketDataBuffer               (app/queue/)
   per-subscriber bounded queues, drop-oldest backpressure
            │
   ┌────────┴─────────────────────┐
   ▼                               ▼
ValidatingStream                MarketAggregator        (app/aggregation/)
(app/validation/)                builds OHLCV candles per symbol,
   │  drops bad ticks             flushes to SQLite every 15 min
   ▼                                       │
MarketProcessor                            ▼
(app/processors/)                   market_data.db (market_candles table)
tracks latest state per symbol              │
   │                                        ▼
   ├──────────────┐              GET /candles/export (CSV download)
   ▼              ▼
REST endpoint  WebSocketGateway   (app/gateways/)
(/market/{symbol})   │
                      ▼
                Connected clients (eventually Symphony)
```

- **`app/connectors/`** — talks to the market-data source. `base_connector.py`
  defines the interface every provider adapter must implement
  (`connect`, `stream`, `disconnect`, `normalize`). `market_connector.py` is
  the current mock implementation. A real provider gets its own class here
  implementing the same interface — nothing else in the app changes.
- **`app/models/`** — `MarketData`, the canonical shape everything downstream
  of a connector deals with: the live tick (`symbol`, `price`, `volume`,
  `timestamp`) plus the quote-page fundamentals (`previous_close`, day/52-week
  range, market cap, bid/ask, dividend info, ...). The mock connector
  generates the fundamentals; a real connector would only need to supply
  what its entitlement actually includes.
- **`app/queue/`** — `MarketDataBuffer` sits between the connector and every
  downstream consumer. Each subscriber gets its own bounded queue; if a
  consumer falls behind and its queue fills up, the oldest buffered tick for
  *that* subscriber is dropped to make room, so one slow consumer can never
  block or slow down another.
- **`app/validation/`** — `validate_tick`/`ValidatingStream` apply business
  rules a schema can't express (bid < ask, price within day/52-week range,
  tick not stale or timestamped in the future). The real-time branch and the
  aggregator each validate independently, so a bad tick never reaches a
  WebSocket client or a candle.
- **`app/processors/`** — consumes the validated real-time feed and keeps the
  latest known value per symbol.
- **`app/aggregation/`** — `MarketAggregator` is a separate branch off the
  buffer (not downstream of the processor), so a slow database write can
  never delay real-time delivery. It builds OHLCV candles per symbol in
  memory and flushes them to a SQLite database (`market_data.db`, table
  `market_candles`) every 15 minutes, plus once more on clean shutdown.
- **`app/gateways/`** — delivery layer. Currently a WebSocket gateway that
  broadcasts to connected clients concurrently; a webhook gateway for
  Symphony workflow events would live here too.
- **`app/main.py`** — wires everything together and exposes the FastAPI app,
  including the `/candles/export` CSV download of aggregated history.
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
.\.venv\Scripts\activate
```

**macOS/Linux:**
```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

You should see `(.venv)` appear at the start of your prompt once it's active,
and `python --version` should report `Python 3.12.x`.

> **Windows: activation blocked?** If PowerShell refuses to run
> `Activate.ps1` ("running scripts is disabled on this system"), the venv is
> **not** active and plain `pip`/`uvicorn` will use your global Python
> instead. Either allow local scripts for your user account only (one-time,
> then reopen PowerShell):
>
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
>
> or skip activation entirely and call the venv's Python directly, as shown
> in the Windows commands below.

### 3. Install dependencies

**With the venv activated (any OS):**
```bash
python -m pip install -r requirements.txt
```

**Windows, without activating** (always installs into `.venv`):
```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If pip prints `Defaulting to user installation because normal site-packages
is not writeable`, it is installing into your global Python, not the venv —
activate the venv or use the `.\.venv\Scripts\python.exe` form above.

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
QUEUE_MAX_SIZE=200
```

Only symbols listed in `MARKET_SYMBOLS` will return data from
`/market/{symbol}` — unlisted symbols return `{"error": "Symbol not found"}`.

## Running the service

From the project root, with the venv activated:

```bash
python -m uvicorn app.main:app --reload
```

**Windows, without activating:**
```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Using `python -m uvicorn` rather than the bare `uvicorn` command guarantees
the venv's Uvicorn is used, and avoids `uvicorn is not recognized` errors
when the venv's `Scripts` folder isn't on your `PATH`.

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
| `http://127.0.0.1:8000/ticker`    | Live quote card UI (`app/static/ticker.html`), driven by the WebSocket feed above |
| `http://127.0.0.1:8000/candles/export` | Historical OHLCV candles as a CSV download (opens in Excel). Optional `?symbol=AAPL` and `?interval=15m` query params |

`127.0.0.1` means "this machine only" — the service isn't reachable from
another computer or from Symphony yet. That's expected during development.

### Quick test with curl

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/market/AAPL
curl http://127.0.0.1:8000/candles/export -o market_candles.csv
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
│   ├── queue/
│   │   └── market_buffer.py       # per-subscriber bounded queues, drop-oldest
│   │
│   ├── validation/
│   │   └── market_validator.py    # business-rule checks + ValidatingStream
│   │
│   ├── processors/
│   │   └── market_processor.py    # tracks latest state per symbol
│   │
│   ├── aggregation/
│   │   └── market_aggregator.py   # OHLCV candles -> SQLite (market_data.db)
│   │
│   ├── gateways/
│   │   └── websocket_gateway.py   # client tracking + concurrent broadcast
│   │
│   └── static/
│       └── ticker.html            # live quote card UI, served at /ticker
│
├── requirements.txt
├── .env.example
├── market_data.db                 # SQLite, created on first run (gitignored)
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
- [x] Bounded queue + backpressure policy for high-throughput feeds
      (`MarketDataBuffer`, drop-oldest per subscriber)
- [x] OHLCV aggregation + persistence, with CSV export of historical candles
- [ ] Observability (connection status, messages/sec, latency, dropped
      messages — `MarketDataBuffer.dropped_counts` already tracks the last
      of these per subscriber and just needs to be surfaced)
