# API and pages

Every URL the service serves, with its parameters and responses. Run the service and open `/docs` for the interactive version.

## Endpoints

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
| `http://127.0.0.1:8000/screener` | The Screener tab (`app/static/screener.html`; Bloomberg's EQS): every company with a profile file, filtered and sorted by its ratios; the filters live in the page's address, so a screen can be bookmarked |
| `http://127.0.0.1:8000/screener/results?pe_max=12&yield_min=4&sort=pe_ratio` | The screener's data: per company `price`, `market_cap`, `pe_ratio`, `price_to_book`, `dividend_yield` and `return_on_equity`, each `{value, formula, reason}` as on its description page. Optional `sector`, `pe_max`, `pb_max`, `yield_min` and `roe_min` (in %), `market_cap_min` (GHS), all inclusive; `sort` is `dividend_yield` (default), `pe_ratio`, `price_to_book`, `return_on_equity`, `market_cap` or `symbol`. A company without a filtered figure is left out and counted per filter in `filters`, with its reasons, never treated as zero. 400 for a sector with no company. See `docs/screener.md` |
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
| `http://127.0.0.1:8000/candles?symbol=MTNGH&range=1D` | OHLCV candles as JSON for charting. `symbol` is required (404 if not in the feed). `range` is `1D`, `5D`, `1M`, `6M`, `YTD`, `1Y`, `5Y` or `Max` (default `1D`) and picks the lookback and candle interval (see `ranges.py` in [architecture.md](architecture.md)); `interval` (`5m`, `15m`, `1h`, `1d`, `1w`) overrides the interval. 400 for an unknown range or interval. Returns `symbol`, `range`, `interval`, `start` and `candles` — each `time`, `open`, `high`, `low`, `close`, `volume` and `yield` (`{open, high, low, close}` for bills and bonds, `null` for equities and price-only corporates). The newest candle is the live, still-open one. `currency` (equities only; 400 for bills and bonds) converts OHLC at the BoG mid: daily and weekly candles each at their day's fix, intraday ones at the latest; `currency` and `fx` in the response say how. `warmup` (0–200, default 0) also returns up to that many earlier candles in a separate `warmup` list (same shape, oldest first), so a moving average or RSI has a value from the range's first candle; empty for `Max`, and shorter where stored history runs out; 422 outside 0–200 |
| `http://127.0.0.1:8000/candles/export` | Stored OHLCV candles from `market_candles` as a CSV download (opens in Excel), ordered by symbol then time. Optional `?symbol=MTNGH` (default: all symbols) and `?interval=` (`5m`, `15m`, `1h`, `1d`, `1w`; default `15m`). Columns: `symbol`, `interval`, `window_start`, `window_end`, `open`, `high`, `low`, `close`, `volume`, `tick_count`, `yield_open`, `yield_high`, `yield_low`, `yield_close`. Reads the database only, so the open window is as of the last flush (up to 60 s old) |

### Fixed income pages

| URL | Purpose |
|---|---|
| `/fixed-income` | The Fixed Income tab: bills and bonds by GFIM segment and sell/buy-backs, live over `/ws/market` |
| `/yield-curve` | The Yield Curve tab: the GoG curve against the previous day, week or month, with the spread analysis from `/spreads` |
| `/curve?date=YYYY-MM-DD` | The GoG yield curve as (tenor in years, closing yield) points from bills and bonds, each with the instruments behind it; `date` picks the latest session on or before it |
| `/bond/{symbol}` | A bill or bond's detail page (a simplified YAS): live quote, price/yield calculator, risk, invoice and history |
| `/bond/{symbol}/analytics` | The bond page's calculator (`app/bond_math/calculator.py`): price ↔ yield, accrued, duration, DV01 and the trade invoice. Optional `settle`, `face` (default 1,000,000), `price` or `yield`. Conventions in [fixed-income-sources-and-conventions.md](fixed-income-sources-and-conventions.md) |

### FX and hedging

| URL | Purpose |
|---|---|
| `/fx` | The FX tab: BoG reference rates, the cross matrix and synthetic forwards |
| `/fx/cross` | The cross-rate matrix at the BoG mid: `matrix[i][j]` is units of `currencies[j]` one unit of `currencies[i]` buys. Optional `currencies` (comma-separated) and `date` |
| `/fx/forwards` | Indicative synthetic forwards for USD, EUR and GBP against the cedi, by covered interest parity (BoG mid as spot, T-bill auction yields for the cedi leg, SOFR/€STR/SONIA flat for the foreign leg) |
| `/hedging` | The Hedging tab: what doing nothing, a forward or an option would cost on a foreign-currency payment or receipt, and an FX swap to roll or move one. `/calculators` redirects here |
| `POST /calc/fx-option` | Prices a European currency option against the cedi with QuantLib (Garman-Kohlhagen): premium, Greeks, forward, break-even, P&L across spot and, given a price, implied volatility. See [fx-option-calculator.md](fx-option-calculator.md) |
| `POST /calc/fx-swap` | An FX swap against the cedi: near and far rates, cedi amounts, swap points, annualised premium and net cash difference |

BoG's rate formats and quirks are in [fx-rates.md](fx-rates.md).

### Commodities

| URL | Purpose |
|---|---|
| `/commodities` | The Commodities tab: live GCX spot contracts and monthly world reference prices in US$ and cedis |
| `/commodity/{symbol}` | A GCX contract's page: live quote, chart, the session and its world benchmark |
| `/gcx/contracts`, `/gcx/contracts/{symbol}` | GCX spot contracts with their latest quote in cedis per tonne (and per 50 kg bag), the session so far and the world benchmark where there is one |
| `/gcx/watchlist/me` | The user's pinned GCX contracts; `PUT` replaces them. Separate from the equity watchlist |
| `/commodities/prices?month=YYYY-MM` | World Bank "Pink Sheet" monthly averages for the commodities offered, in US$ as published and in cedis at that month's average BoG USD mid |
| `/commodities/{code}/history` | One commodity's monthly averages, oldest first; optional `since` |

Details: [commodities-gcx.md](commodities-gcx.md) (the GCX contracts) and [commodity-prices.md](commodity-prices.md) (the reference prices).

### Trade tickets and notifications

| URL | Purpose |
|---|---|
| `/tickets` (page) | The Trade Tickets window: write a ticket, file it, print it and follow it to the depository's reply. Opened from the Fixed Income tab and bond pages with `?symbol=`, `&price=` and `&quantity=` |
| `POST /tickets/preview` | What `POST /tickets` would write, without saving: settlement date and amounts |
| `POST /tickets` | Writes up a trade agreed with a counterparty as a draft. Equities settle T+`EQUITY_SETTLEMENT_LAG`, bills and bonds T+2 with the bond calculator's invoice, GCX contracts T+1 |
| `/tickets/me`, `/tickets/{id}` | The user's tickets (as trader or counterparty), newest first; one ticket for its parties only |
| `POST /tickets/{id}/file` | Sends a draft to the depository (the CSD): filed or rejected directly, or sent by email when `DEPOSITORY_MODE=email` |
| `POST /tickets/{id}/confirmation` | Records the depository's emailed reply; both parties are notified |
| `/tickets/{id}/pdf` | The ticket as a printable A4 PDF (`download=true` to save it) |
| `/notifications/me` | The user's ticket notifications, newest first (`unread=true` for unread only); `POST /notifications/me/{id}/read` marks one read |
| `ws://…/ws/notifications` | The same notifications, live. Protocol in `app/gateways/notification_gateway.py` |
| `/identity/me` | Who the requesting user is; `PUT` sets the ID for this browser. A stand-in until Symphony sign-in, no more trusted than `X-User-Id` |

### Chart drawings

| URL | Purpose |
|---|---|
| `/drawings/me` | The charts the user has drawn on, with how many drawings each |
| `/drawings/me/{symbol}` | The user's trend lines, levels and Fibonacci retracements on a stock chart; `PUT` replaces them (an empty list clears them) |

`127.0.0.1` means "this machine only" — the service isn't reachable from
another computer or from Symphony yet. That's expected during development.

## Quick test with curl

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

## The live WebSocket feed

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

