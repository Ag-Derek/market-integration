# Market Integration Service

A market-data service for Ghana's markets. It takes in a live price
feed, puts every price into one standard format, and serves it to web
pages and other systems over REST and WebSocket (and, later, Symphony).

There is no real data provider yet. The equity, bond and commodity
feeds are **mock** feeds, calibrated from real Ghana Stock Exchange
(GSE) and Ghana Fixed Income Market (GFIM) daily reports, so the whole
pipeline can be built and tested now. The Bank of Ghana FX rates,
T-bill auction results, economic calendar and World Bank commodity
prices are **real**, pulled from their publishers.

## What's in it

| Area | What you get |
|---|---|
| **Equities** | Live quotes for all 42 GSE stocks, a stock page with charts, indicators, drawing tools, seasonality and a company description (profile, officers, financials, ratios), movers and per-user watchlists |
| **Fixed income** | Bills and bonds by GFIM segment, the GoG yield curve with rich/cheap spreads, and a bond page with a price/yield calculator |
| **Auctions and calendar** | Bank of Ghana T-bill auction results, a new-issue monitor, and an economic calendar (MPC decisions, GSS releases, holidays) |
| **FX** | Bank of Ghana interbank rates, cross rates, synthetic forwards, prices in any of 16 currencies, and a hedging page (forwards, options, FX swaps) |
| **Commodities** | Ghana Commodity Exchange spot contracts and monthly world reference prices |
| **Trade tickets** | Write up a trade, file it with the depository, print it, and get notified of the outcome |
| **Analysis** | A multi-series chart rebased to 100 with a correlation matrix |

## Quick start

You need Python 3.10 or later (3.12 recommended). From the repository
root:

```powershell
cd market-integration
py -3.12 -m venv .venv                                  # macOS/Linux: python3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt   # macOS/Linux: .venv/bin/python -m pip ...
Copy-Item .env.example .env                             # macOS/Linux: cp .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Then open <http://127.0.0.1:8000/ticker>. Press `Ctrl+C` to stop.

The GSE only trades on weekdays from 10:00 to 15:00 GMT, and the mock
follows that. To see prices move at other times, set
`MARKET_SESSION_OVERRIDE=open` in `.env`.

Run the tests with `python -m pytest` from the `market-integration`
folder.

Trouble with the virtual environment, every setting in `.env`, and how
to keep FX rates current while the service is off are all in
[docs/setup.md](market-integration/docs/setup.md).

## Pages

| Page | What it shows |
|---|---|
| `/ticker` | Live quote cards for your watchlist |
| `/stock/{symbol}` | One stock: chart, indicators, drawings, seasonality, description |
| `/fixed-income`, `/yield-curve`, `/bond/{symbol}` | Bills and bonds, the yield curve, one bond |
| `/auctions`, `/calendar` | T-bill auctions, the economic calendar |
| `/fx`, `/hedging` | FX rates and forwards, hedging calculators |
| `/commodities`, `/commodity/{symbol}` | GCX contracts and world prices |
| `/graph` | Several series on one chart, with correlations |
| `/tickets` | Trade tickets |
| `/docs` | Interactive API documentation (Swagger) |

Every REST and WebSocket endpoint is described in
[docs/api.md](market-integration/docs/api.md). `/health` and `/metrics`
report the service's state.

## How it works

```text
mock feeds (later a real provider)
        │
        ▼
  connector ─► buffer ─┬─► validation ─► processor ─► REST + WebSocket ─► pages
                       │
                       └─► validation ─► aggregator ─► candles in SQLite ─► charts
```

The buffer gives each branch its own queue, so a slow database write
never delays a live quote, and a slow client never costs the charts a
tick. Swapping in a real provider means writing one new connector class;
nothing downstream changes. The details are in
[docs/architecture.md](market-integration/docs/architecture.md).

## Documentation

All in `market-integration/docs/`:

- [architecture.md](market-integration/docs/architecture.md): how the parts fit together, the project layout, and adding a real provider
- [setup.md](market-integration/docs/setup.md): full setup, configuration and load testing
- [api.md](market-integration/docs/api.md): every endpoint and the WebSocket protocol
- [roadmap.md](market-integration/docs/roadmap.md): what's done and what's next
- [data-formats.md](market-integration/docs/data-formats.md): the GSE and GFIM daily reports and their quirks
- [fixed-income-sources-and-conventions.md](market-integration/docs/fixed-income-sources-and-conventions.md): bond maths conventions
- [fx-rates.md](market-integration/docs/fx-rates.md), [fx-option-calculator.md](market-integration/docs/fx-option-calculator.md): FX data and the option calculator
- [commodities-gcx.md](market-integration/docs/commodities-gcx.md), [commodity-prices.md](market-integration/docs/commodity-prices.md): commodity contracts and prices
- [company-data-sources.md](market-integration/docs/company-data-sources.md): where the company profiles come from
- [benchmarks.md](market-integration/docs/benchmarks.md): throughput results
