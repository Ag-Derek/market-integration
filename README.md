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
     CompositeConnector             (app/connectors/)
     equities + fixed income as one feed
            │                 │
            │ live ticks      │ fetch_history() — once, at startup
            ▼                 └───────────────────────────────┐
     MarketDataBuffer               (app/queue/)              │
   per-subscriber bounded queues, drop-oldest backpressure    │
            │                                                 │
   ┌────────┴──────── two independent subscribers ───┐        │
   │  "processor" queue                 "aggregator" queue    │
   ▼                                                 ▼        ▼
ValidatingStream                         MarketAggregator    (app/aggregation/)
(app/validation/)                        validates on its own; backfill()
   │  drops bad ticks                    seeds history, then live ticks
   ▼                                     build 5m/15m/1h/1d/1w candles
MarketProcessor                          in memory, flushed every 60 s
(app/processors/)                                    │
tracks latest state per symbol                       ▼
   │                                 market_data.db (market_candles,
   ├───────────────┐                                 repo_trades)
   ▼               ▼                                 │
REST endpoints  WebSocketGateway  (app/gateways/)    ├─► GET /candles  (JSON for charts;
(/market/…,     /ws/market                           │   overlays the in-memory
 /search, …)       │                                 │   open windows)
                   ▼                                 └─► GET /candles/export (CSV)
            Connected clients: /ticker, /stock/{symbol},
            /fixed-income, … (eventually Symphony)
```

The two branches each get their own queue from `MarketDataBuffer`, so
neither can hold up the other: a slow SQLite write never delays a
WebSocket client, and a slow client never costs the aggregator a tick.
Each branch validates ticks itself.

- **`app/connectors/`** — talks to the market-data source. `base_connector.py`
  defines the interface every provider adapter must implement
  (`connect`, `stream`, `disconnect`, `normalize`, and `fetch_history`
  for backfill). `market_connector.py` is
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
  every active equity, bill and bond; suspended names (ALW, PBC) are
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
  what its entitlement actually includes. `candle.py` holds the `Candle`
  model (OHLCV, `tick_count` — 0 for backfilled candles — and the optional
  yield OHLC for fixed income), the supported intervals (`INTERVALS`), and
  the time grid: `bucket_start()` aligns every window to the UTC epoch
  (`1d` is UTC midnight to midnight; `1w` starts Monday 00:00 UTC). Both
  backfill and live aggregation use it, so a backfilled candle and a live
  one for the same window share a `window_start` and merge cleanly.
  `resample()` rolls finer candles up into coarser ones (the mocks build
  their weekly history from daily this way).
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
  never delay real-time delivery. It builds OHLCV candles per symbol on
  every interval at once (`5m`, `15m`, `1h`, `1d`, `1w`) in memory and
  flushes them to a SQLite database (`market_data.db`, table
  `market_candles`) every 60 seconds (`DEFAULT_FLUSH_INTERVAL_SECONDS`),
  plus once more on clean shutdown. Each flush writes the closed windows
  and a snapshot of the still-open ones; `INSERT OR REPLACE` on
  `(symbol, interval, window_start)` keeps re-flushing idempotent.
  `/candles` overlays the in-memory windows on the stored rows, so the
  newest candle is always current, not as of the last flush.
  - **Backfill.** On startup, before any live ticks flow,
    `aggregator.backfill(connector)` asks the connector for each symbol's
    history on every interval (`fetch_history()`, part of the connector
    interface) and replaces whatever is stored from the start of that span
    onward: the provider is the source of truth for the past. If the
    newest fetched candle is the still-open bucket, it seeds the live
    window with it, so e.g. today's daily candle keeps its pre-startup
    open/high/low. Bad candles are dropped (and logged) by
    `validate_candle` before they reach the database.
  - **`ranges.py`** — the chart range selector. `RANGES` maps each range
    to the candle interval it is drawn from (coarsest that still gives
    roughly 100–750 points) and its lookback; `HISTORY_DEPTH` is how far
    back backfill fetches per interval so every range is fully covered,
    plus `MAX_WARMUP` (200) candles ahead of it for the chart's
    indicators (`/candles?warmup=`):

    | Range | Interval | Lookback |
    |---|---|---|
    | `1D` | `5m` | 1 day |
    | `5D` | `15m` | 5 days |
    | `1M` | `1h` | 30 days |
    | `6M` | `1d` | 182 days |
    | `YTD` | `1d` | since 1 Jan (UTC) |
    | `1Y` | `1d` | 365 days |
    | `5Y` | `1w` | 5 years |
    | `Max` | `1w` | everything |

    | Interval | Backfill depth |
    |---|---|
    | `5m` | 2 days (1D + 288 warm-up candles) |
    | `15m` | 10 days (5D + 480) |
    | `1h` | 44 days (1M + 336) |
    | `1d` | everything the provider has (the Seasonality view reads every year) |
    | `1w` | everything the provider has |

  - Volume: the mock reports *cumulative* session volume, so each tick
    contributes its increase over the previous tick, not its raw value.
  - Fixed income: bills and bonds are charted on clean closing price,
    and each candle also carries the same window in yield
    (`yield_open` … `yield_close`). Sell/buy-back (repo) ticks never make
    candles; they go to a `repo_trades` table, one row per bond per
    session.
- **`app/fx/`** — currency conversion at the Bank of Ghana's interbank
  rates. Unlike market data, these are real: `bog.py` pulls them from
  BoG's website (there's no API: it asks the history table's AJAX
  endpoint, as the page does), `store.py` keeps every fix as published
  in SQLite (`fx_rates`), and `rates.py` serves lookups: the latest fix,
  or the one in force on a day. The first start pulls 11 years of
  history in the background; after that an hourly pull fetches only
  what's new. Prices stay in cedis everywhere; the pages convert live
  quotes at the latest fix, and `/candles?currency=` converts history at
  each day's. BoG's formats and quirks (inverted West African pairs, a
  rescaled leone) are in `docs/fx-rates.md`.
- **`app/auctions/`** — the Bank of Ghana's weekly T-bill auction results,
  also real: `bog.py` pulls BoG's Treasury Bill Rates table the same way
  as the FX rates (the shared wpDataTables client is `app/bog.py`),
  `store.py` keeps every result in SQLite (`auction_results`),
  `tracker.py` keeps it current (the whole table on the first start, a few
  seconds; then the last four weeks, hourly, `AUCTION_REFRESH_SECONDS`, 0
  never pulls), and `monitor.py` builds the new-issue monitor from it and
  the GFIM report. If BoG can't be reached, stored results stay in use and
  `/health` says why under `auctions`.
- **`app/econ/`** — the economic calendar. Two sources are pulled, every
  6 hours (`ECON_REFRESH_SECONDS`; 0 never pulls) and kept in SQLite:
  BoG's policy rate history (`bog.py`: every MPC decision since 2002,
  read from the data behind BoG's Policy Rate Trends chart) and the Ghana
  Statistical Service's release calendar (`gss.py`: the year's CPI, GDP,
  MIEG, PPI ... releases, read from the data behind GSS's Release
  Calendar page; GSS gives the week of the month, never the day). Coming
  MPC meetings are kept by hand in `data/economic_calendar.json` from
  BoG's yearly PDF: add next year's when it is published (December).
  `events.py` merges these with the T-bill auctions and market holidays.
  Each source fails on its own; `/health` says which under
  `economic_calendar`.
- **`app/gateways/`** — delivery layer. Currently a WebSocket gateway that
  broadcasts to connected clients concurrently; a webhook gateway for
  Symphony workflow events would live here too.
- **`app/main.py`** — wires everything together and exposes the FastAPI app,
  including `/candles` (chart data), the `/candles/export` CSV download of
  aggregated history, and the `/stock/{symbol}` page. Startup order is:
  seed instruments → connect → backfill → start buffer → start aggregator
  and processor.
- **`app/config.py`** — environment-driven settings (tracked symbols, mock
  update interval, queue size, provider URL/key placeholders).

## Requirements

- Python 3.10+ (3.12 recommended)
- Windows PowerShell, macOS/Linux shell — instructions below cover both
- Node.js (optional) — only to run the chart indicator tests
  (`tests/test_indicators.py` runs `indicators.js` under Node); they're
  skipped without it

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

Bills and bonds come from a separate mock (the GFIM market) in the same
feed. Its quote rates and a burst mode for load tests are set with:

```env
FI_MOCK_INTERVAL_SECONDS=0.5
FI_QUOTE_INTERVALS=treasury_bill=2,GHGGOG069931=0.5
FI_BURST_RATE=0
```

`FI_QUOTE_INTERVALS` sets the seconds between two-way quotes by segment
(`new_gog`, `ddep`, `old_gog`, `treasury_bill`, `corporate`) or by ISIN;
anything not listed keeps the defaults in
`app/connectors/fixed_income_connector.py`, and 0 means "only when it
trades". `FI_BURST_RATE` > 0 quotes every bill and bond that many times a
second, in session or not, for throughput tests. Otherwise the GFIM mock
trades in the GFIM session, 09:00–16:00 GMT (`FI_SESSION_OPEN`,
`FI_SESSION_CLOSE`), on the equity calendar's trading days.

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

If a feed drops, it reconnects on its own, without a restart
(`app/connectors/supervisor.py`). Equities and fixed income are
supervised separately, so one dropping doesn't stop the other. Retry
*n* waits `RECONNECT_INITIAL_DELAY_SECONDS` (default 1) × 2^(*n*−1), capped
at `RECONNECT_MAX_DELAY_SECONDS` (default 30), with up to
`RECONNECT_JITTER` (default 0.5) of each delay randomised. While it
retries, `/health` returns 503 with `"status": "reconnecting"` and each
feed's state under `feeds`. The status message's `feed.state` is
`"reconnecting"`, and `feed.reconnect` gives the attempt and next retry
time. Clients are sent a status message the moment a feed drops or
recovers, and the pages' badges then read "Reconnecting…".

To show prices in another currency, the service pulls the Bank of
Ghana's interbank FX rates from bog.gov.gh, so it needs internet access
for that (and only that). The first start fetches 11 years of history in
the background, about a minute; later starts and the hourly check
fetch only new fixes. `FX_REFRESH_SECONDS` (default 3600) sets how often
it checks; 0 never pulls, for offline work, and the pages then stay in
cedis unless rates were stored earlier. If BoG can't be reached, the
service keeps converting at the rates it already has and `/health` says
why under `fx`.

The service only pulls while it's running. To keep the rates current
when it isn't, schedule the same pull as a daily job, `python -m
app.fx.pull` (one pull, then exits; `--log FILE` to log to a file;
once today's fix is stored it doesn't contact BoG). On Windows, from the
project root, this registers it with Task Scheduler at 09:00 and hourly
16:00–22:00 Ghana time, and whenever you log on or unlock the PC, so a
laptop that's often asleep still gets the day's fix; it logs to
`fx-pull.log`:

```powershell
.\scripts\register-fx-pull-task.ps1                          # -GhanaTimes 09:00,17:00 for others
Start-ScheduledTask -TaskName "market-integration BoG FX pull"  # run it now
.\scripts\register-fx-pull-task.ps1 -Remove                  # take it off
```

On macOS/Linux, a crontab line (the times are the machine's):

```text
0 9,16-22 * * * cd /path/to/market-integration && .venv/bin/python -m app.fx.pull --log fx-pull.log
```

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
| `http://127.0.0.1:8000/health`    | Health check: 200 `healthy`, or 503 `reconnecting` (a feed is being retried) / `unhealthy`; `feeds` gives each feed's reconnect state, `metrics` a summary of `/metrics`, `fx` the Bank of Ghana rates' latest fix and last pull (`last_error` if it failed; never makes the service unhealthy) |
| `http://127.0.0.1:8000/metrics`   | Pipeline metrics in Prometheus text format (scrape it directly); `?format=json` for the same as JSON. Ticks received; dropped ticks and queue depth per buffer subscriber; WebSocket clients and subscriptions; ticks passed/rejected by each validating consumer, and rejections by rule (`crossed_book`, `stale`, `matured`, …); the aggregator's flush count and last flush time, duration and rows; each feed's up/reconnect state |
| `http://127.0.0.1:8000/instruments` | Instrument master as JSON. Optional `?asset_class=equity\|bill\|bond` and `?sector=Banking` (case-insensitive) filters |
| `http://127.0.0.1:8000/instruments/{symbol}` | One instrument's reference data (e.g. `/instruments/MTNGH`); 404 if unknown |
| `http://127.0.0.1:8000/instruments/{symbol}/description` | Everything an equity's description page needs: instrument, company profile, officers, annual financials (from `data/companies/`, one file per company, every value as `{value, source, as_of}`, null when not filled in) and figures calculated from them, the live quote and the stored daily candles: market cap, P/E, P/B, dividend yield, return on equity, 1-day, YTD and 52-week figures, 12-month total return and beta against the simulated GSE-CI (each with its formula, and a reason when null). 404 for unknown symbols and for bills and bonds |
| `http://127.0.0.1:8000/indices/gse-ci` | The simulated GSE Composite Index used for beta: daily levels from every equity's daily closes, equal-weighted, from 1,000. A stand-in until the feed carries the published index |
| `http://127.0.0.1:8000/graph` | The Graph tab (`app/static/graph.html`; Bloomberg's G and CORR): up to eight series on one chart, each rebased to 100 at the start of the range (1M–Max), with a crosshair tooltip giving each one's actual value, and below it their correlation matrix (daily or weekly). Add stocks, bills and bonds (their yield), GSE-CI and currencies from the picker; the series and range are kept in the address (`?series=MTNGH,GSE-CI,FX:USD&range=1Y`), so a graph can be shared |
| `http://127.0.0.1:8000/graph/series?series=MTNGH,GSE-CI&range=1Y` | The chart's data (`app/analytics/graph.py`). `series` is up to 8 comma-separated ids: an equity symbol (daily close, GHS), a bill or bond ISIN (closing yield; clean price for price-only corporates), `GSE-CI` (the simulated index, over all stored history) or `FX:USD` etc. (Bank of Ghana mid, cedis per unit). `range` is `1M`, `3M`, `6M`, `YTD`, `1Y` (default), `2Y`, `5Y` or `Max`; `5Y` and `Max` give one point a week. Per series: `kind`, `unit`, `base` (the last value on or before the start, or the first one, flagged `late_start`), `last`, `observed_days` of `days` (how thinly it trades) and `points`, each `[date, value, rebased]`. 400 for a bad range or more than 8 series, 404 for an unknown id, 400/503 for a currency we don't offer or have no rates for yet |
| `http://127.0.0.1:8000/graph/correlation?series=MTNGH,GCB,GSE-CI&range=1Y` | The correlation matrix of the same series: Pearson correlation of changes (returns; the change in yield for yields), each pair measured between the days *both* were observed, so a thin stock's no-trade days (a carried close: zero, then the whole move at once) don't dilute it. `frequency=weekly` uses week-on-week changes. Each cell is `{value, observations}`; `value` is null below `min_observations` (20). Same ids, ranges and errors as `/graph/series` |
| `http://127.0.0.1:8000/search?q=gc` | Typeahead for the search bar: `symbol`, `name`, `asset_class`, `price` and `change` (null until quoted). Matches symbol, name, ISIN, tenor, issuer and maturity date (`?q=2027`), ignoring case and punctuation (`?q=fan milk`); ranked exact symbol, symbol prefix, name word prefix, then substring. Optional `limit` (1–50, default 10), `asset_class` and `sector` (case-insensitive; with an empty `q` it lists the whole sector) |
| `http://127.0.0.1:8000/sectors` | Every sector in the instrument master with its instrument count, for the search bar's Sector filter |
| `http://127.0.0.1:8000/watchlists/me` | The requesting user's pinned equities, in order: `symbols`, `max_pins` (`WATCHLIST_MAX_PINS`, default 12) and `is_default` (true until the user saves one; a new user gets `WATCHLIST_DEFAULT`). `PUT` with `{"symbols": [...]}` replaces the whole list; unknown symbols, bills and bonds, duplicates or too many pins get a 422 naming them. Until Symphony sign-in, the user is the `X-User-Id` header if sent, else an anonymous ID kept in the `mi_user` cookie (`app/identity.py`) — an identifier, not authentication. Saved in the SQLite database, so watchlists survive restarts |
| `http://127.0.0.1:8000/fx/rates` | The Bank of Ghana interbank rates prices are converted at: per currency, `ghs_per_unit` and `per_ghs` at the mid, BoG's `buying` and `selling`, the `date` it published them and its `source_pair` (e.g. `USDGHS`, `GHSNGN`). The latest fix, or with `?date=2026-01-15` the one in force that day. 16 currencies (USD, EUR, GBP, CHF, JPY, CNY, CAD, AUD, NZD, ZAR, DKK, NOK, SEK, NGN, XOF, GMD). 503 until the first pull from BoG has landed; 404 for a date before the history held |
| `http://127.0.0.1:8000/fx/convert?amount=100&to=USD` | Converts `amount` from `from` (default GHS) to `to` at the BoG mid; latest fix, or `?date=` for a past one. Between two foreign currencies it goes through the cedi. 400 for an unknown currency |
| `http://127.0.0.1:8000/movers?type=gainers` | Today's movers among equities: `type` is `gainers` or `losers` (by % change, VWAP vs previous close) or `active` (by shares traded). Optional `limit` (1–50, default 10), `sector`, and `q` to search within the list |
| `http://127.0.0.1:8000/fixed-income/report` | Today's fixed-income report in the shape of the GFIM daily trading report: every section plus the summary. Blank report cells are `null` |
| `http://127.0.0.1:8000/spreads` | Spread analysis for a session (`app/analytics/spreads.py`), shown on the Yield Curve tab (`/yield-curve`): a Nelson-Siegel-Svensson curve fitted to the closing yields of bills, New GoG and 2023 DDEP bonds (`curve`: factors, the two decays, RMSE overall and per segment, and `samples` for drawing it); each government point's spread to it in bp (`government`; positive is cheap, negative rich; Old GoG measured but not fitted to, as their closes are often stale); each corporate's yield worked out from its closing price, as a semi-annual ACT/ACT bullet, and its spread over the curve (`corporate`; null with a `reason` where there's no coupon); and spreads by issuer, widest first (`issuers`). Corporate spreads are indicative: GFIM publishes no corporate yields and we have no term sheets. `date` as for `/curve`; 400 for a date after the current session |
| `http://127.0.0.1:8000/auctions` | The Auctions tab (`app/static/auctions.html`; a new-issue monitor for the GoG primary market): the latest T-bill auction's yields with their move on the week, the next tender (estimated), the auction yield history by tenor (1Y–Max), the recent new bills against their GFIM close, and every result BoG has published |
| `http://127.0.0.1:8000/auctions/monitor` | The new-issue monitor's data (`app/auctions/monitor.py`): `latest` (the latest tender's 91/182/364-day bills: discount and interest rate, `change_bp` from the previous auction), `new_issues` (bills from the last four tenders, each matched to its GFIM bill by tenor and maturity, with `secondary_spread_bp`: GFIM close less the auction yield), `next_auction` (tender, expected auction and issue dates from the Friday/Monday pattern moved round holidays, `status` `scheduled`/`today`/`results_pending`, and the GFIM bills `maturing` on that issue date) and `last_bond_auction`. 503 until the first pull from BoG lands |
| `http://127.0.0.1:8000/auctions/results?security=91%20DAY%20BILL` | Auction results as the Bank of Ghana published them, newest first, each with its change from the previous auction of that security. Optional `security` (BoG's name, case-insensitive: `91 DAY BILL`, `2 YR FXR NOTE`, ...; 400 if never auctioned), `since` (YYYY-MM-DD) and `limit` (1–5000, default 500). 503 until the first pull lands |
| `http://127.0.0.1:8000/calendar` | The Calendar tab (`app/static/calendar.html`; Bloomberg's ECO for Ghana): the policy rate, the next MPC decision, CPI and GDP release at a glance; every event by day with importance, status, actual, prior and change, filtered by range, importance and category; and the policy rate since 2002 |
| `http://127.0.0.1:8000/economic-calendar?start=2026-10-01&end=2026-12-31` | The calendar's events (`app/econ/events.py`), by date: BoG policy rate decisions (`monetary_policy`; past ones with the rate set, prior and move, coming ones from the MPC dates with the rate in force as prior), GSS releases (`prices`, `growth`, `trade`, `labour`, `statistics`; `precision` `week` with `date`..`date_end`), T-bill auctions (`auction`; yields and moves, the next one `estimated`) and market holidays (`holiday`). Each has `importance` (high/medium/low) and `status`: `done`, `due` (today, inside its week, or the MPC sitting), `upcoming` or `overdue` (past its date or week, not shown done by the source). Optional `category` (comma-separated) and `importance` (the lowest to include). Default range a week back to 60 days on; at most 400 days. Also `policy_rate` (the rate in force, since when, and its last move) and `sources` (each pull's state) |
| `http://127.0.0.1:8000/economic-calendar/policy-rate` | Every BoG policy rate decision since Nov 2002, oldest first, with its move in bp; optional `since`. Empty until the first pull |
| `http://127.0.0.1:8000/fixed-income/summary` | Volume, number of trades and largest trade per section, plus grand totals |
| `http://127.0.0.1:8000/fixed-income/{section}` | One section's rows: `new_gog`, `ddep`, `old_gog`, `treasury_bill`, `corporate` or `sell_buy_back` |
| `http://127.0.0.1:8000/market/status` | Market session and feed status: `status` is `open`, `pre_open` or `closed` (with a `reason`: `weekend`, `holiday`, `before_hours`, `after_hours`), plus `next_open`, `next_close`, `last_close`, the feed's heartbeat, and `badge` (`live`, `delayed`, `closed` or `disconnected`) |
| `http://127.0.0.1:8000/market/{symbol}` | Latest snapshot for a tracked symbol (e.g. `/market/MTNGH`); 404 if unknown |
| `ws://127.0.0.1:8000/ws/market`   | WebSocket — live quotes for the symbols a client subscribes to (protocol below) |
| `http://127.0.0.1:8000/ticker`    | Live quote card UI (`app/static/ticker.html`), driven by the WebSocket feed above. The cards are your watchlist (`/watchlists/me`), in your order: pin from the search bar, unpin with ✕, reorder by dragging a card's ⠿ handle (or focusing it and using the arrow keys); the collapsible sidebar lists every listed equity, pinned ones highlighted with their day change; click another to pin it. Only pinned symbols are subscribed. The header search (press `/`; ↑/↓, Enter to open, Shift+Enter to pin, Esc to close) is also on `/stock/{symbol}`. The currency picker (top right, also on `/stock/{symbol}`) shows prices in another currency at the latest BoG mid; it's remembered per browser |
| `http://127.0.0.1:8000/stock/{symbol}` | Single-stock page (`app/static/stock.html`) for an equity: live quote over `/ws/market` plus a chart from `/candles` with the 1D–Max range selector. The **Indicators** menu next to the ranges adds SMA (two), EMA and Bollinger bands on the price, and RSI and MACD in panes below the volume, each with editable periods; they're calculated in the browser (`app/static/indicators.js`) on closes, follow live ticks, show in the hover tooltip, and are remembered per browser. The **Seasonality** view (`#seasonality`, from `/seasonality/{symbol}`) shows the average return for each calendar month with how many years it rose, this year's path from 1 January against past years and their average, and the table of monthly returns. 404 for bills, bonds and unknown symbols. `/stock` redirects to the first equity |
| `http://127.0.0.1:8000/seasonality/{symbol}` | An equity's seasonality from every stored daily close, in cedis (`app/company/seasonality.py`): `months` (each month's return, last close against the previous month's), `annual`, `by_month` (per calendar month: `mean`, `median`, `positive` of `count`, `best` and `worst` year), `paths` (each year's cumulative return by day of year, from the previous year's last close) and `average` (the mean path of the finished years, `average_years` of them). The current month and year are to date, flagged `partial` and left out of the averages; a month with no previous month to measure from has a null return. 404 for unknown symbols, bills and bonds; empty for a suspended equity |
| `http://127.0.0.1:8000/candles?symbol=MTNGH&range=1D` | OHLCV candles as JSON for charting. `symbol` is required (404 if not in the feed). `range` is `1D`, `5D`, `1M`, `6M`, `YTD`, `1Y`, `5Y` or `Max` (default `1D`) and picks the lookback and candle interval (see `ranges.py` above); `interval` (`5m`, `15m`, `1h`, `1d`, `1w`) overrides the interval. 400 for an unknown range or interval. Returns `symbol`, `range`, `interval`, `start` and `candles` — each `time`, `open`, `high`, `low`, `close`, `volume` and `yield` (`{open, high, low, close}` for bills and bonds, `null` for equities and price-only corporates). The newest candle is the live, still-open one. `currency` (equities only; 400 for bills and bonds) converts OHLC at the BoG mid: daily and weekly candles each at their day's fix, intraday ones at the latest; `currency` and `fx` in the response say how. `warmup` (0–200, default 0) also returns up to that many earlier candles in a separate `warmup` list (same shape, oldest first), so a moving average or RSI has a value from the range's first candle; empty for `Max`, and shorter where stored history runs out; 422 outside 0–200 |
| `http://127.0.0.1:8000/candles/export` | Stored OHLCV candles from `market_candles` as a CSV download (opens in Excel), ordered by symbol then time. Optional `?symbol=MTNGH` (default: all symbols) and `?interval=` (`5m`, `15m`, `1h`, `1d`, `1w`; default `15m`). Columns: `symbol`, `interval`, `window_start`, `window_end`, `open`, `high`, `low`, `close`, `volume`, `tick_count`, `yield_open`, `yield_high`, `yield_low`, `yield_close`. Reads the database only, so the open window is as of the last flush (up to 60 s old) |

`127.0.0.1` means "this machine only" — the service isn't reachable from
another computer or from Symphony yet. That's expected during development.

### Quick test with curl

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/market/status
curl http://127.0.0.1:8000/market/MTNGH
curl "http://127.0.0.1:8000/candles?symbol=MTNGH&range=5D"
curl "http://127.0.0.1:8000/candles?symbol=MTNGH&range=1Y&warmup=200"
curl http://127.0.0.1:8000/seasonality/MTNGH
curl http://127.0.0.1:8000/fx/rates
curl "http://127.0.0.1:8000/fx/convert?amount=100&to=USD"
curl "http://127.0.0.1:8000/candles?symbol=MTNGH&range=5Y&currency=USD"
curl "http://127.0.0.1:8000/candles/export?symbol=MTNGH&interval=1d" -o market_candles_MTNGH.csv
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
| server → client | `{"type": "ticks", "data": [{...}, ...]}` | The latest quote for each subscribed symbol that changed since the last one, batched: at most one of these per `WS_SEND_INTERVAL_MS` (250 ms by default), however fast the feed runs. In each quote, `timestamp` is when the feed published it; `last_trade_at` is when the symbol last traded, which for a thin name can be days ago. |
| server → client | `{"type": "status", "status": "open", "badge": "live", ...}` | Every few seconds, to every client: the same body as `/market/status`. If these stop arriving, treat the connection as lost. |
| server → client | `{"type": "error", "code": "bad_json" \| "bad_request" \| "unknown_symbols", "message": "...", "symbols": [...]}` | Your message couldn't be used. Unknown symbols are listed; the valid ones in the same request still apply. |

The "Live" badge on the pages comes from `badge`, and from the page's own
connection (Disconnected when the socket drops or status updates stop).
It never depends on price direction. Feed freshness is judged on the
connector's heartbeat, not on the last trade.

Updates are throttled and conflated: nobody can read more than a few
prices a second per instrument, so between two `ticks` messages a newer
quote for a symbol replaces the older one, and a client gets a bounded
number of messages a second at any tick rate. A slow client never holds
up the feed or other clients either; it skips intermediate prices and
always catches up to the latest one for each symbol (see
`app/gateways/websocket_gateway.py`). Candles still see every tick: the
aggregator reads the feed unconflated.

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

### Load testing and benchmarks

`FEED_MODE=load` replaces the GSE equities mock with synthetic
instruments at a fixed tick rate, with optional bursts (the `LOAD_*`
settings in `.env.example`). To measure how far the service goes, run

```bash
python -m bench.benchmark
```

from the `market-integration` folder. It steps through tick rates
against the real service with simulated WebSocket clients, and writes
the results, including the maximum sustained rate and what fails
first, to `docs/benchmarks.md`. See that file for targets and method.

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
│   ├── company/                   # company profiles for description pages: seed, SQLite store, calculated figures, seasonality
│   ├── watchlists/                # per-user pinned equities: validation, SQLite store behind /watchlists/me
│   ├── identity.py                # who's asking: X-User-Id or anonymous cookie, until Symphony auth
│   ├── metrics.py                 # /metrics snapshot + Prometheus rendering
│   │
│   ├── models/
│   │   ├── tick.py                # Tick: equity quote | bill/bond quote | repo trades, by tick_type
│   │   ├── market_data.py         # canonical MarketData schema
│   │   ├── candle.py              # OHLCV candle, INTERVALS, time-bucket grid, resample()
│   │   ├── fixed_income.py        # GFIM report rows (bonds, bills, corporates, sell/buy-backs)
│   │   └── instrument.py          # Instrument reference-data schema
│   │
│   ├── instruments/
│   │   ├── __init__.py            # loads data/instruments.json (instrument master)
│   │   ├── search.py              # in-memory typeahead index behind /search
│   │   └── store.py               # instruments SQLite table behind /instruments
│   │
│   ├── connectors/
│   │   ├── base_connector.py      # interface every provider must implement
│   │   ├── market_connector.py    # mock equity provider (current)
│   │   ├── fixed_income_mock.py   # mock GFIM fixed-income market (current)
│   │   ├── fixed_income_connector.py  # streams it: quote rates, burst mode
│   │   ├── yield_curve.py         # drifting Nelson-Siegel curve behind it
│   │   ├── composite_connector.py # equities + fixed income as one feed
│   │   ├── supervisor.py          # reconnects a dropped feed, with backoff + jitter
│   │   └── gse_mock_profiles.py   # mock calibration fields + defaults
│   │
│   ├── queue/
│   │   └── market_buffer.py       # per-subscriber bounded queues, drop-oldest
│   │
│   ├── validation/
│   │   └── market_validator.py    # business-rule checks + ValidatingStream
│   │
│   ├── processors/
│   │   ├── market_processor.py    # tracks latest state per symbol
│   │   └── movers.py              # equity gainers / losers / most active behind /movers
│   │
│   ├── bond_math/                 # Ghana fixed-income math (% a year, per 100 face)
│   │   ├── __init__.py            # public API of the package
│   │   ├── bills.py               # T-bills: simple-interest ACT/364 price <-> yield, risk
│   │   ├── bonds.py               # coupon bonds: price <-> yield, accrued, duration, DV01
│   │   ├── conventions.py         # day counts, coupon calendar, T+2 settlement date
│   │   ├── invoice.py             # trade invoice: principal + accrued = total
│   │   └── calculator.py          # bond page calculator behind /bond/{symbol}/analytics
│   │
│   ├── aggregation/
│   │   ├── market_aggregator.py   # backfill + live OHLCV candles -> SQLite (market_data.db)
│   │   └── ranges.py              # chart ranges -> candle interval, backfill depth
│   │
│   ├── bog.py                     # reads Bank of Ghana website tables (FX rates, auctions)
│   │
│   ├── econ/
│   │   ├── __init__.py            # PolicyDecision, GssRelease (week windows), MPC seed file
│   │   ├── bog.py                 # pulls BoG's policy rate history
│   │   ├── gss.py                 # pulls GSS's release calendar
│   │   ├── store.py               # policy_rate_decisions, gss_releases SQLite tables
│   │   ├── tracker.py             # keeps both current, each on its own
│   │   └── events.py              # merges MPC, GSS, auctions, holidays into calendar events
│   │
│   ├── auctions/
│   │   ├── __init__.py            # AuctionResult, parsing BoG's rows
│   │   ├── bog.py                 # pulls the Treasury Bill Rates table
│   │   ├── store.py               # auction_results SQLite table
│   │   ├── tracker.py             # keeps it current, lookups by security
│   │   └── monitor.py             # new-issue monitor: latest, vs GFIM, next tender
│   │
│   ├── analytics/
│   │   ├── graph.py               # /graph: rebasing to 100, correlation between days both traded
│   │   └── spreads.py             # /spreads: Svensson fit to the GoG curve, rich/cheap, corporate spreads
│   │
│   ├── gateways/
│   │   └── websocket_gateway.py   # client tracking + concurrent broadcast
│   │
│   ├── session/
│   │   ├── calendar.py            # trading calendar: session hours, holidays, open/closed
│   │   └── status.py              # session + feed heartbeat -> /market/status, badge
│   │
│   └── static/
│       ├── search.js              # header search bar (autocomplete over /search) on /ticker and /stock
│       ├── ticker.html            # live quote card UI, served at /ticker
│       ├── stock.html             # single-stock page + range chart and indicators, served at /stock/{symbol}
│       ├── indicators.js          # SMA, EMA, Bollinger, RSI (Wilder), MACD for the stock chart
│       ├── seasonality.js         # the stock page's Seasonality view (#seasonality)
│       ├── fixed_income.html      # bills and bonds by GFIM segment, served at /fixed-income
│       ├── yield_curve.html       # GoG yield curve vs. a past day, fitted curve, corporates and spreads, served at /yield-curve
│       ├── auctions.html          # T-bill auctions and new issues, served at /auctions
│       ├── calendar.html          # economic calendar and policy rate, served at /calendar
│       ├── graph.html             # several series rebased to 100 + correlation matrix, served at /graph
│       └── bond.html              # bill/bond page (simplified YAS), served at /bond/{symbol}
│
├── data/
│   ├── instruments.json           # instrument master seed (source of truth)
│   ├── companies/                 # one <SYMBOL>.json per company: profile, officers, financials (each value sourced)
│   ├── economic_calendar.json     # BoG MPC meeting dates, from BoG's yearly PDF
│   └── market_calendar.json       # GSE session hours + Ghana public holidays
│
├── docs/
│   ├── data-formats.md            # GSE/GFIM report fields -> our models, data quirks
│   ├── fixed-income-sources-and-conventions.md  # day counts, settlement, yield conventions behind bond_math/
│   ├── benchmarks.md              # throughput targets, latest results, history
│   └── samples/                   # official daily reports for 28-Sep-2026 (xlsx + pdf)
│
├── bench/
│   ├── benchmark.py               # one-command throughput benchmark -> docs/benchmarks.md
│   └── profile_hot_path.py        # per-tick cost by stage + cProfile of the pipeline
│
├── tests/                         # pytest suite (pipeline, candles, bond math, gateway, ...)
│
├── requirements.txt
├── requirements-dev.txt           # test and benchmark dependencies
├── pytest.ini
├── .env.example
├── market_data.db                 # SQLite, created on first run (gitignored)
└── README.md
```

## Replacing the mock with a real provider

1. Create a new class in `app/connectors/` (e.g. `real_market_connector.py`)
   implementing `BaseMarketConnector`: `connect()` (auth + subscribe),
   `stream()` (yield normalized `MarketData`), `disconnect()`,
   `normalize()` (map the provider's raw fields to `MarketData`), and
   `fetch_history()` (historical candles for backfill, bucketed with
   `bucket_start()` from `app/models/candle.py`).
2. Swap the import in `app/main.py` from `MockMarketConnector` to the new
   class.
3. Add any provider-specific settings (URL, API key, symbol format) to
   `app/config.py` and `.env`.

Nothing in `processors/`, `gateways/`, or `main.py`'s wiring needs to
change — that's the point of the connector interface.

## Roadmap

- [ ] Real market-data provider connector (pending provider selection)
- [x] T-bill auction tracker and new-issue monitor (`/auctions`), from
      the Bank of Ghana's published rates. Still to do: auction amounts
      (offered, bid, accepted; BoG press releases only) and BoG's
      quarterly issuance calendar (PDF)
- [x] Economic calendar (`/calendar`): BoG MPC decisions and policy
      rate history, GSS release calendar, auctions, holidays. Still to
      do: the released figures for GSS data (CPI, GDP; GSS publishes them
      only in bulletins and page layout), and implied odds of a rate move
      (needs a market in rate futures or OIS, which Ghana doesn't have)
- [ ] Webhook/event gateway for Symphony workflow triggers (e.g. threshold
      crossings, % change alerts)
- [x] Reconnect/backoff logic for the connector
- [x] Bounded queue + backpressure policy for high-throughput feeds
      (`MarketDataBuffer`, drop-oldest per subscriber)
- [x] OHLCV aggregation + persistence, with CSV export of historical candles
- Charting and analytics (Bloomberg GP/GIP, G, SEAG, CORR equivalents):
  - [x] Technical indicators on the stock chart: SMA, EMA, Bollinger, RSI, MACD
  - [x] Seasonality view on the stock page (`#seasonality`): average
        return by month, year-over-year lines, monthly returns table
  - [x] `/graph` page: several series (equities, GSE-CI, bond yields, FX)
        rebased to 100 on one chart
  - [x] Correlation matrix of the selected series, on returns, measured
        between days both traded (`/graph/correlation`)
  - [x] Spread analysis (`/spreads`, on the Yield Curve tab): fitted GoG
        curve, rich/cheap per bill and bond, corporate spreads by bond and
        issuer. Z-spread and OAS still to do (zero curve, term sheets)
  - [ ] Drawing tools on the stock chart (trend lines, levels, Fibonacci),
        saved per user
  - [ ] Finer intraday (1-minute candles or stored ticks)
  - [ ] Adjusted history for corporate actions (splits, bonus issues),
        once real multi-year history is loaded
- [ ] Observability: connection status, dropped messages, queue depth,
      clients and validation rejections are on `/metrics` (messages/sec is
      `rate(market_buffer_received_ticks_total[1m])`); end-to-end latency
      is still to do
