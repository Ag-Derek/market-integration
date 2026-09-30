# Market Integration Service

A market-data integration service that ingests a live price feed, normalizes
it into a canonical format, and exposes it for consumption (initially via
REST/WebSocket, eventually via Symphony webhooks).

Currently wired to a **mock** provider that simulates live trading in
every GSE-listed equity (plus the GLD ETF), calibrated from real GSE
daily reports: real price levels, one-sided or empty order books, and
thinly traded names that go days without a trade. That lets the full
pipeline — connector → buffer →
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
  the current mock implementation; `gse_mock_profiles.py` documents the
  per-symbol calibration it reads (starting price, volatility, trade
  frequency, book shape). A real provider gets its own class here
  implementing the same interface — nothing else in the app changes.
- **`app/instruments/`** — the instrument master. The source of truth is
  the checked-in seed file `data/instruments.json`: every tradable symbol
  with its reference data (name, asset class, sector, currency, ISIN,
  status) plus an optional `mock` block calibrating the simulator. On
  startup it is copied into the `instruments` SQLite table that
  `/instruments` serves. Config, the connector and the API all take their
  symbol universe from here, so **adding an entry to the seed file puts
  the symbol in the feed, the API and the UI after a restart, with no
  code change**. The seed holds all 42 GSE equities plus the 161
  government bonds, T-bills and corporate bonds from the GFIM sample
  report (fixed income uses the ISIN as its symbol). The feed carries
  only active equities; bills, bonds and suspended names (ALW, PBC) are
  listed by `/instruments` but not streamed.
- **`docs/`** — `data-formats.md` maps every field of the official GSE and
  GFIM daily reports to our models and records their data quirks, for
  whoever builds the real GSE connector. The sample reports it's based on
  are in `docs/samples/`.
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

Open `.env` and adjust if you want to track only some symbols:

```env
MARKET_SYMBOLS=MTNGH,GCB,SCB
MOCK_INTERVAL_SECONDS=0.5
QUEUE_MAX_SIZE=200
```

Leave `MARKET_SYMBOLS` empty to track every active equity in the
instrument master (`data/instruments.json`: 40 of the 42 GSE equities;
ALW and PBC are suspended). A symbol
that isn't in the instrument master, or that the feed can't carry
(a bill, bond, or suspended/delisted name), stops the service at startup
with an error naming it. Untracked symbols get a 404 from
`/market/{symbol}` and `/candles`.

The GSE only trades in a fixed session on weekdays, and the mock follows
`data/market_calendar.json` (session hours, trading days and Ghana public
holidays; `MARKET_CALENDAR_PATH` points elsewhere). Outside the session
the mock doesn't trade, so to see prices move in the evening or at the
weekend, set:

```env
MARKET_SESSION_OVERRIDE=open
```

The session hours come from the GSE Trading Rules (pre-open 09:30–10:00,
continuous auction 10:00–15:00 GMT). The holiday list is kept by hand: add each year's gazetted dates
(including the two Eids, announced shortly before) as they are published.
`FEED_STALE_SECONDS` (default 15) is how long the feed may go without a
heartbeat before the badge shows "Delayed", and `STATUS_INTERVAL_SECONDS`
(default 5) how often WebSocket clients get a status update.

`MARKET_DB_PATH` (default `market_data.db`) sets where the SQLite
database holding the instruments table and aggregated candles lives.
The test suite points it at a temporary file, so running `pytest` never
touches a running dev server's database.

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
| `http://127.0.0.1:8000/instruments` | Instrument master as JSON. Optional `?asset_class=equity\|bill\|bond` and `?sector=Banking` (case-insensitive) filters |
| `http://127.0.0.1:8000/instruments/{symbol}` | One instrument's reference data (e.g. `/instruments/MTNGH`); 404 if unknown |
| `http://127.0.0.1:8000/fixed-income/report` | Today's fixed-income report in the shape of the GFIM daily trading report: every section plus the summary. Blank report cells are `null` |
| `http://127.0.0.1:8000/fixed-income/summary` | Volume, number of trades and largest trade per section, plus grand totals |
| `http://127.0.0.1:8000/fixed-income/{section}` | One section's rows: `new_gog`, `ddep`, `old_gog`, `treasury_bill`, `corporate` or `sell_buy_back` |
| `http://127.0.0.1:8000/market/status` | Market session and feed status: `status` is `open`, `pre_open` or `closed` (with a `reason`: `weekend`, `holiday`, `before_hours`, `after_hours`), plus `next_open`, `next_close`, `last_close`, the feed's heartbeat, and `badge` (`live`, `delayed`, `closed` or `disconnected`) |
| `http://127.0.0.1:8000/market/{symbol}` | Latest snapshot for a tracked symbol (e.g. `/market/MTNGH`); 404 if unknown |
| `ws://127.0.0.1:8000/ws/market`   | WebSocket — live quotes for the symbols a client subscribes to (protocol below) |
| `http://127.0.0.1:8000/ticker`    | Live quote card UI (`app/static/ticker.html`), driven by the WebSocket feed above |
| `http://127.0.0.1:8000/candles/export` | Historical OHLCV candles as a CSV download (opens in Excel). Optional `?symbol=MTNGH` and `?interval=15m` query params |

`127.0.0.1` means "this machine only" — the service isn't reachable from
another computer or from Symphony yet. That's expected during development.

### Quick test with curl

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/market/status
curl http://127.0.0.1:8000/market/MTNGH
curl http://127.0.0.1:8000/candles/export -o market_candles.csv
```

### The live WebSocket feed

`/ws/market` sends each client only the symbols it subscribes to. All
messages are JSON:

| Direction | Message | Meaning |
|---|---|---|
| server → client | `{"type": "welcome", "symbols": [...], "status": {...}}` | Sent on connect: the symbols you can subscribe to and the current market status. No prices; a client that never subscribes gets nothing but status updates. |
| client → server | `{"action": "subscribe", "symbols": ["MTNGH", "GCB"]}` | Start receiving these symbols (case-insensitive). |
| client → server | `{"action": "unsubscribe", "symbols": ["GCB"]}` | Stop receiving them. |
| server → client | `{"type": "subscribed" \| "unsubscribed", "symbols": [...], "subscriptions": [...]}` | Confirmation: what changed, and everything you're now subscribed to. |
| server → client | `{"type": "snapshot", "data": {"MTNGH": {...}}}` | Current quotes for the symbols you just subscribed to. |
| server → client | `{"type": "tick", "data": {...}}` | A new quote for a subscribed symbol. `timestamp` is when the feed published it; `last_trade_at` is when the symbol last traded, which for a thin name can be days ago. |
| server → client | `{"type": "status", "status": "open", "badge": "live", ...}` | Every few seconds, to every client: the same body as `/market/status`. If these stop arriving, treat the connection as lost. |
| server → client | `{"type": "error", "code": "bad_json" \| "bad_request" \| "unknown_symbols", "message": "...", "symbols": [...]}` | Your message couldn't be used. Unknown symbols are listed; the valid ones in the same request still apply. |

The "Live" badge on the pages comes from `badge`, and from the page's own
connection (Disconnected when the socket drops or status updates stop).
It never depends on price direction. Feed freshness is judged on the
connector's heartbeat, not on the last trade.

A slow client never holds up the feed or other clients. It skips
intermediate prices and always catches up to the latest one for each
symbol (see `app/gateways/websocket_gateway.py`).

To try it, save this as `test-client.html` and open it in a browser while
the server is running:

```html
<!DOCTYPE html>
<html>
<head><title>Market Feed</title></head>
<body>
  <h1>Live Market Data</h1>
  <pre id="market"></pre>
  <script>
    const socket = new WebSocket("ws://127.0.0.1:8000/ws/market");
    socket.onopen = () => {
      socket.send(JSON.stringify({ action: "subscribe", symbols: ["MTNGH", "GCB"] }));
    };
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
│   │   ├── market_data.py         # canonical MarketData schema
│   │   ├── candle.py              # OHLCV candle + time-bucket grid
│   │   ├── fixed_income.py        # GFIM report rows (bonds, bills, corporates, sell/buy-backs)
│   │   └── instrument.py          # Instrument reference-data schema
│   │
│   ├── instruments/
│   │   ├── __init__.py            # loads data/instruments.json (instrument master)
│   │   └── store.py               # instruments SQLite table behind /instruments
│   │
│   ├── connectors/
│   │   ├── base_connector.py      # interface every provider must implement
│   │   ├── market_connector.py    # mock equity provider (current)
│   │   ├── fixed_income_mock.py   # mock GFIM fixed-income market (current)
│   │   └── gse_mock_profiles.py   # mock calibration fields + defaults
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
│   ├── session/
│   │   ├── calendar.py            # trading calendar: session hours, holidays, open/closed
│   │   └── status.py              # session + feed heartbeat -> /market/status, badge
│   │
│   └── static/
│       └── ticker.html            # live quote card UI, served at /ticker
│
├── data/
│   ├── instruments.json           # instrument master seed (source of truth)
│   └── market_calendar.json       # GSE session hours + Ghana public holidays
│
├── docs/
│   ├── data-formats.md            # GSE/GFIM report fields -> our models, data quirks
│   └── samples/                   # official daily reports for 28-Sep-2026 (xlsx + pdf)
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
