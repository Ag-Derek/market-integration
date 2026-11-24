# Setup and configuration

The full setup guide. The [README](../../README.md) has the short version.

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

## Load testing and benchmarks

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
