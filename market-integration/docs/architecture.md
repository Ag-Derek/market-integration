# Architecture

How the service is put together. For setup see [setup.md](setup.md); for every endpoint see [api.md](api.md).

## Overview

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
  report (fixed income uses the ISIN as its symbol) and 17 Ghana
  Commodity Exchange (GCX) spot contracts. The feed carries every active
  equity, bill, bond and GCX contract; suspended names (ALW, PBC) are
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
- **`app/commodities/`** — commodity prices: the World Bank's monthly
  reference prices (kept as published, shown in US$ and cedis) and the
  GCX spot contracts set against them. See `commodity-prices.md` and
  `commodities-gcx.md`.
- **`app/derivatives/`** — calculators for contracts the service doesn't
  trade: an FX option priced with QuantLib and an FX swap built on the
  synthetic forwards in `app/fx/forwards.py`. See
  `fx-option-calculator.md`.
- **`app/tickets/`** — trade tickets: a trader writes up an agreed trade,
  files it with the depository (`app/connectors/depository.py`) and both
  parties are notified of the outcome.
- **`app/drawings/`** — per-user chart drawings (trend lines, levels,
  Fibonacci) on the stock page.
- **`app/gateways/`** — delivery layer: the market-data WebSocket
  gateway, which broadcasts to connected clients concurrently, and the
  per-user notification gateway behind `/ws/notifications`. A webhook
  gateway for Symphony workflow events would live here too.
- **`app/main.py`** — wires everything together and exposes the FastAPI app,
  including `/candles` (chart data), the `/candles/export` CSV download of
  aggregated history, and the `/stock/{symbol}` page. Startup order is:
  seed instruments → connect → backfill → start buffer → start aggregator
  and processor.
- **`app/config.py`** — environment-driven settings (tracked symbols, mock
  update interval, queue size, provider URL/key placeholders).

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
│   │   ├── gcx_mock.py            # mock Ghana Commodity Exchange spot contracts
│   │   ├── load_connector.py      # synthetic equities at a fixed tick rate (FEED_MODE=load)
│   │   ├── depository.py          # files trade tickets with the CSD (mock, direct or email)
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
│   ├── fx/
│   │   ├── bog.py                 # pulls BoG's interbank FX fixes
│   │   ├── store.py               # fx_rates SQLite table, every fix as published
│   │   ├── rates.py               # latest fix / fix in force on a day, conversion
│   │   ├── forwards.py            # cross rates and synthetic forwards (covered interest parity)
│   │   ├── overnight.py           # overnight rates (SOFR, €STR, SONIA) for the forwards
│   │   └── pull.py                # one-off pull for a scheduled job (python -m app.fx.pull)
│   │
│   ├── commodities/
│   │   ├── worldbank.py           # pulls the World Bank Pink Sheet (monthly prices)
│   │   ├── store.py               # keeps every series as published
│   │   ├── prices.py              # the commodities offered, in US$ and cedis
│   │   └── gcx.py                 # GCX spot contracts against their world benchmark
│   │
│   ├── derivatives/
│   │   ├── fx_option.py           # FX option pricing (QuantLib, Garman-Kohlhagen) behind /calc/fx-option
│   │   └── fx_swap.py             # FX swap behind /calc/fx-swap
│   │
│   ├── tickets/
│   │   ├── desk.py                # write, file and confirm trade tickets; notify both parties
│   │   ├── store.py               # tickets and notifications SQLite tables
│   │   └── pdf.py                 # printable A4 ticket
│   │
│   ├── drawings/
│   │   └── store.py               # per-user chart drawings behind /drawings/me
│   │
│   ├── analytics/
│   │   ├── graph.py               # /graph: rebasing to 100, correlation between days both traded
│   │   └── spreads.py             # /spreads: Svensson fit to the GoG curve, rich/cheap, corporate spreads
│   │
│   ├── gateways/
│   │   ├── websocket_gateway.py   # client tracking + concurrent broadcast
│   │   └── notification_gateway.py  # per-user ticket notifications over /ws/notifications
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
│       ├── bond.html              # bill/bond page (simplified YAS), served at /bond/{symbol}
│       ├── fx.html                # BoG rates, cross matrix, synthetic forwards, served at /fx
│       ├── hedging.html           # forward vs option vs doing nothing, FX swaps, served at /hedging
│       ├── commodities.html       # GCX contracts + world reference prices, served at /commodities
│       ├── commodity.html         # one GCX contract, served at /commodity/{symbol}
│       ├── tickets.html           # trade tickets and notifications, served at /tickets
│       ├── description.js         # the stock page's Description view (profile, ratios, revenue & EPS)
│       ├── drawings.js            # the stock chart's drawing tools
│       ├── currency.js            # display-currency picker and price formatting
│       ├── strip.js               # market strip under the page header (GSE-CI, USD/GHS, ...)
│       └── render.js              # rendering helpers for live-updating pages
│
├── data/
│   ├── instruments.json           # instrument master seed (source of truth)
│   ├── companies/                 # one <SYMBOL>.json per company: profile, officers, financials (each value sourced)
│   ├── economic_calendar.json     # BoG MPC meeting dates, from BoG's yearly PDF
│   └── market_calendar.json       # GSE session hours + Ghana public holidays
│
├── docs/                          # see the documentation list in the README
│   └── samples/                   # official daily reports for 28-Sep-2026 (xlsx + pdf)
│
├── bench/
│   ├── benchmark.py               # one-command throughput benchmark -> docs/benchmarks.md
│   └── profile_hot_path.py        # per-tick cost by stage + cProfile of the pipeline
│
├── benchmarks/
│   └── candle_fidelity.py         # checks candles built under load against the ticks sent
│
├── tests/                         # pytest suite (pipeline, candles, bond math, gateway, ...)
│
├── requirements.txt
├── requirements-dev.txt           # test and benchmark dependencies
├── pytest.ini
├── .env.example
└── market_data.db                 # SQLite, created on first run (gitignored)
```

The README sits one level up, at the repository root.

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
