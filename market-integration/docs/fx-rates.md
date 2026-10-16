# Bank of Ghana FX rates

Every price in the service is in cedis. To show prices in another currency, the
service converts them at the interbank rates the Bank of Ghana (BoG) publishes
each business day. This page covers where the rates come from and how we pull
them, the quirks in BoG's data, how prices are converted, and the FX page's
cross rates and synthetic forwards. The code is in `app/fx/`.

## Source

BoG publishes the rates on two pages:

| Page | What it shows |
|---|---|
| [Daily Interbank FX Rates](https://www.bog.gov.gh/treasury-and-the-markets/daily-interbank-fx-rates/) | The latest fix only |
| [Historical Interbank FX Rates](https://www.bog.gov.gh/treasury-and-the-markets/historical-interbank-fx-rates/) | Every fix since 2 Jan 1996, about 145,000 rows |

BoG offers no API, CSV or Excel export. Both pages are WordPress
[wpDataTables](https://wpdatatables.com/) tables. The historical table (id 40)
is filled in by the browser: it POSTs DataTables server-side parameters to
`https://www.bog.gov.gh/wp-admin/admin-ajax.php?action=get_wdtable&table_id=40`,
and the reply is JSON:

```json
{"draw": 1, "recordsTotal": "145407", "recordsFiltered": "145407",
 "data": [["05 Oct 2026", "US Dollar", "USDGHS", "11.7591", "11.7709", "11.7650"], ...]}
```

Each row is date, currency, pair, buying, selling, mid. `app/fx/bog.py` sends
the same request the page does: date column (0) descending, a page of 2,000
rows at a time. This one source covers both the backfill and the daily
updates, because its newest rows are the current fix.

The request needs the `wdtNonce` value. The page puts it in a hidden input,
`wdtNonceFrontendServerSide_40`. The nonce isn't tied to a cookie, but
WordPress rotates it, and a stale nonce gets an **empty 200**, not an error.
So when a reply isn't the table's JSON, the client reads the nonce from the
page again and retries once.

As measured on 6 Oct 2026, a 2,000-row page takes BoG 1–2 seconds.

## Pulling and storing

- **Storage.** Rows go into the `fx_rates` SQLite table exactly as BoG
  publishes them, keyed by `(date, pair)`, so re-fetching an overlap replaces
  rows instead of duplicating them. The table is never rebuilt, so the
  history survives restarts.
- **First start.** Pulls 11 years of history, which covers every chart
  range: the mock's daily history is 10 years, and a weekly candle uses the
  fix for its last day. That's about 30 requests, roughly a minute. It runs in
  the background, and each page is stored as it arrives.
- **After that.** Every `FX_REFRESH_SECONDS` (default 3600), it pulls from the
  latest stored day onwards, usually one request. BoG publishes once a
  business day, so a new fix is picked up within the hour.
- **Incomplete history.** If the stored history doesn't reach back 11 years
  (a backfill failed part-way, say), the next pull starts from the beginning
  again.
- **Failures.** A failed pull is logged, shown in `/health` under
  `fx.last_error`, and retried after 5 minutes. Rates already stored stay in
  use, so a BoG outage only makes them older. `fx` is not one of `/health`'s
  components: the market data service is still healthy without it.
- **Offline.** `FX_REFRESH_SECONDS=0` never pulls, and conversion uses
  whatever is stored. The tests run this way and never call BoG.
- **Scheduled job.** The service only pulls while it's running.
  `python -m app.fx.pull` (`app/fx/pull.py`) does one pull of the same kind
  into the same database and exits: 0 on success, 1 on failure. Once today's
  fix is stored, it ends without contacting BoG. On Windows,
  `scripts/register-fx-pull-task.ps1` registers it with Task Scheduler for
  09:00 and hourly 16:00–22:00 Ghana time, and for every log on and unlock.
  The job and the running service can both write the same days; that's
  harmless. A running service picks up the job's rows at its own next
  refresh, or at startup if `FX_REFRESH_SECONDS=0`.
  - **Why so many runs:** the first version ran at 18:00 only. On 7 Oct 2026
    the laptop was in Modern Standby at 18:00 and only woke for seconds
    afterwards, so the day's fix waited until the next morning's run. A
    sleeping PC runs no scheduled tasks; more chances, unlocks especially,
    fix that at no cost.
- **When BoG publishes.** BoG states no time. 7 Oct 2026's fix wasn't out at
  12:25 GMT and was there by 09:00 the next day. `fx_rates.fetched_at` keeps
  when each day's fix was *first* fetched, and the job's log says `New fix:
  <day>, first seen <time>`, so the publication time can be read off within
  an hour. Query the database with:

  ```sql
  SELECT date, MIN(fetched_at) FROM fx_rates GROUP BY date ORDER BY date DESC LIMIT 10;
  ```

  Rows pulled as part of the history only show when they were backfilled.

## Data quirks

| Quirk | Handling |
|---|---|
| Most pairs are cedis per unit of the other currency (`USDGHS` 11.765). The West African currencies are the other way round: `GHSNGN` 112.95 means naira per cedi. Also `GHSXOF`, `GHSGMD`, `GHSMRO` and `GHSSLL`. | `normalize()` turns every pair into cedis per unit. When it inverts a pair it swaps buying and selling, so buying stays the lower rate. |
| **Leone:** on 11 Sep 2026 BoG switched `GHSSLL` from old leones (2,109 per cedi) to the redenominated leone (2.11), but kept the old code. Its history jumps 1000×. | Not offered. Offering it would need the old rows divided by 1,000 and the code changed to SLE. |
| **Ouguiya:** `GHSMRO` is still quoted in the pre-2018 ouguiya (MRO), a unit that no longer exists. The current MRU is worth 10 MRO. The series is consistent; it just uses a withdrawn unit. | Not offered. Offering it as MRU would need the rows divided by 10. |
| **ECOWAS** (`WAUGHS`) is the West African Unit of Account, not a currency. | Not offered. |
| Rates have 4 decimal places, so small ones are coarse: `JPYGHS` 0.0745 is only good to about ±0.07%. | Accepted. |
| Values before the July 2007 redenomination are in old cedis: `USDGHS` was 1,450 in 1996. | Out of range: we keep 11 years. |
| Numbers may carry thousands separators (`1,437.2700`). Names aren't consistent (`Japanese yen` and `Japanese Yen`; `BCEAO` for the CFA franc). | Separators are stripped. We key on the pair and use our own currency names. |
| No fixes on weekends or Ghana public holidays. | A day without a fix uses the latest fix before it. |

All three rows we don't offer are still stored, so offering one later needs
only the change described above, not a new pull. The 16 currencies offered are
listed in `app/fx/__init__.py`.

## How prices are converted

All conversions use BoG's **mid** rate.

- **Live quotes** (ticker cards, the stock page's quote and stats, search
  results) are converted in the browser by `app/static/currency.js`, at the
  latest fix. Price and previous close use the same rate, so the percentage
  change is the shares' own change in cedis.
- **Daily and weekly candles** (`/candles?…&currency=USD` for the 6M to Max
  ranges) are converted on the server, each at the fix in force on its day; a
  weekly candle uses its last day's fix. A 5-year chart in dollars therefore
  shows what the shares were worth in dollars at the time, including the
  cedi's own moves.
- **Intraday candles** (1D, 5D, 1M) are all converted at the latest fix. BoG
  fixes once a day, so converting each intraday candle at its own day's rate
  would draw steps at midnight that aren't price moves.
- **Not converted:**
  - Bills and bonds, which are quoted per 100 of face value.
  - The stock page's Description view, where company financials stay in the
    currency they were reported in. The page says so when another currency is
    selected.

## The FX page

`/fx` shows three things built on the fixes. The code is in `app/fx/forwards.py`
(the maths) and `app/fx/overnight.py` (the foreign rates).

### Reference rates (`/fx/rates`)

BoG's buying, selling and mid for each currency, as cedis per unit and units
per cedi. Each row also has the fix before it and the move since
(`previous_date`, `previous_mid`, `change_percent`). The move is in cedis per
unit, so a positive number means the cedi weakened. A `date` gives the fix in
force that day.

### Cross rates (`/fx/cross`)

BoG fixes against the cedi only, so every cross goes through it: units of B
per A = (GH₵ per A) ÷ (GH₵ per B), at the mid. The matrix is consistent by
construction: A→B→C equals A→C. `currencies=USD,EUR,...` picks the rows and
columns (the cedi and every currency by default), and `date` works as on
`/fx/rates`.

### Synthetic forwards (`/fx/forwards`)

We can't read a traded cedi forward curve, so these are what covered interest
parity implies:

    F = S × (1 + r_GHS × d / 364) / (1 + r_FOR × d / basis)

| Input | Source | Convention |
|---|---|---|
| S, spot | BoG mid, latest fix | GH₵ per unit |
| r_GHS | Latest 91, 182 and 364-day T-bill auction yields (`app/auctions/`), interpolated linearly in days, flat outside 91–364 | Simple, ACT/364, as GFIM quotes bills |
| r_FOR | USD: SOFR (NY Fed). EUR: €STR (ECB). GBP: SONIA (BoE). The latest published rate, held flat for the whole tenor | Simple. ACT/360 for SOFR and €STR, ACT/365 for SONIA |

Tenors are 1W, 1M, 2M, 3M, 6M, 9M and 1Y, with value dates counted from the
fix date. The reply gives the outright, the points (outright − spot, in pips
of 0.0001) and the premium (`(F/S − 1) × 365/d`, %): the cedi depreciation the
rates imply. Every input comes back with its date.

These are indicative mids, not dealer quotes. The simplifications:

- The foreign leg is the overnight rate held flat, with no term premium and
  no expected policy moves. Further out, that matters more.
- The cedi leg is the T-bill curve, which a bank would not necessarily
  borrow or lend at.
- Value dates ignore the spot lag (T+2) and holidays.
- Only USD, EUR and GBP have forwards. We hold no interest rate for the other
  13 currencies. Adding one means adding its overnight benchmark to
  `app/fx/overnight.py`.

**Pulling the overnight rates.** All three publishers have free, keyless
downloads:

| Benchmark | URL | Format |
|---|---|---|
| SOFR | `https://markets.newyorkfed.org/api/rates/secured/sofr/last/30.json` | JSON, `refRates[].effectiveDate` / `percentRate` |
| €STR | `https://data-api.ecb.europa.eu/service/data/EST/B.EU000A2X2A25.WT?lastNObservations=30&format=csvdata` | CSV, `TIME_PERIOD` / `OBS_VALUE` |
| SONIA | Bank of England database, series `IUDSOIA`, as CSV | CSV, `DATE` (`14 Oct 2026`) / `IUDSOIA` |

Every `OVERNIGHT_REFRESH_SECONDS` (default 3600) the service fetches about
the last month of each into the `overnight_rates` table. The upsert means a
revised rate replaces the old one and a missed day is filled in. Each
benchmark is pulled on its own: if one publisher fails, the error shows in
`/health` under `overnight_rates`, the others still update, and the stored
rate stays in use. `0` never pulls (the tests run this way). SOFR for a day is
published the next business morning in New York, so it usually runs a day
behind the BoG fix.
