# GSE and GFIM daily report formats

We use simulated data until the GSE API is available. This page maps every
field in the two official daily reports to our models, so the mock, the UI and
the models are built for real data. It also records the data quirks the real
connector will have to handle.

This is a one-time reference. We don't import these reports on an ongoing
basis.

Fixed income sources, quoting and pricing conventions (day count, settlement,
bill yield vs discount rate) are in
[fixed-income-sources-and-conventions.md](fixed-income-sources-and-conventions.md).

## Samples

Both samples are for Monday 28 September 2026, in `docs/samples/`:

| File | Report |
|---|---|
| `gse-daily-shares-etfs-2026-09-28.xlsx` / `.pdf` | GSE "Daily Shares & ETFs" (equities) |
| `gfim-trading-report-2026-09-28.xlsx` / `.pdf` | GFIM "Trading Report" (fixed income) |

Treat the `.xlsx` files as the originals. The PDFs are printouts of them, but
some values differ because the workbooks use formulas that recalculate on the
day the file is opened (see [quirks](#quirks)).

Field status in the tables below:

- **✓**: in our models today.
- **—**: deliberately not modelled; the reason is given.

---

## Equities: GSE Daily Shares & ETFs

One sheet, one row per security, 13 columns. The sample has 25 rows (see
[quirk 1](#quirks)).

| Report column | Our field | Status | Notes |
|---|---|---|---|
| Daily Date | `MarketData.timestamp` | ✓ | Text `DD/MM/YYYY`, not an Excel date. `timestamp` is when the feed published the quote, so for the end-of-day report it is when the report was received; the session's close goes in `last_trade_at` for names that traded. |
| Share Code | `symbol` | ✓ | May carry `**` markers (`**ALW**`, `PBC**`). Strip them and map to `Instrument.status` (see quirk 2). |
| Year High (GH¢) | `year_high` | ✓ | Calendar year or trailing 52 weeks is unconfirmed (see [open questions](#open-questions-for-the-gse)). The mock uses the calendar year. |
| Year Low (GH¢) | `year_low` | ✓ | As above. |
| Previous Closing Price - VWAP (GH¢) | `previous_close` | ✓ | This is the **previous session's VWAP**, not its last trade (quirk 3). |
| Opening Price (GH¢) | `open` | ✓ | In the sample it equals the previous closing VWAP on every row, including names that traded. It looks like a reference price rather than a first trade. |
| Last Transaction Price (GH¢) | `price` | ✓ | The last trade. Can differ from the closing price (CAL 0.70, ETI 1.66, …). |
| Closing Price - VWAP (GH¢) | `vwap` | ✓ | The **official closing price** is the session VWAP (quirk 3). |
| Price Change (GH¢) | `change` | ✓ | Closing VWAP − previous closing VWAP (MTNGH: 6.54 − 6.22 = 0.32). It isn't `price − previous_close`. |
| Closing Bid Price (GH¢) | `bid` | ✓ | Often blank (quirk 5). Blank is `None`. |
| Closing Offer Price (GH¢) | `ask` | ✓ | Often blank (quirk 5). Blank is `None`. |
| Total Shares Traded | `volume` | ✓ | 0 on no-trade days (6 of 25 names in the sample). |
| Total Value Traded (GH¢) | `value_traded` | ✓ | Turnover in GHS. About closing VWAP × shares traded; not exact because the published VWAP is rounded to 2 dp (CAL: 0.70 × 295,749 = 207,024 vs 207,887.93). |

How the mock (`MockMarketConnector`) reproduces the report:

- `previous_close` is the previous session's VWAP. A session with no trades
  carries the last traded day's VWAP over.
- `open` equals `previous_close`, as on every row of the sample.
- `vwap` is turnover ÷ shares for the session so far. With no trades yet it
  equals `previous_close`, and `change` and `value_traded` are 0.
- Thin names go whole sessions without a trade, and books are two-sided, bid
  only, offer only or empty, per the sample. A lone bid or offer can sit a few
  ticks on the wrong side of the last price (quirk 5).
- Because the mock streams a live session, `vwap`, `change` and
  `value_traded` are values *so far*; at the close they are the report's
  closing values.
- It only trades during the session in `data/market_calendar.json`. At each
  open it starts a new session: the last session's VWAP becomes
  `previous_close` and `open`, and volume starts again from 0. Its
  backfilled history still runs around the clock.

Fields in our models that the report does **not** have:

| Our field | Why we keep it |
|---|---|
| `bid_size`, `ask_size` | Order-book depth; expected from the live API. 0 when a side is empty. |
| `day_high`, `day_low` | Intraday range; expected from the live API. |
| `last_trade_at` | When the symbol last traded; expected from the live API. Separate from `timestamp` (when the quote was published) because a thin name's last trade can be days old on a healthy feed. `None` if it never traded. |
| `week52_high`, `week52_low` | Trailing 52-week range. Kept alongside `year_high`/`year_low` until the GSE confirms what "Year" means; drop one pair then. |
| `name`, `exchange_label` | From the instrument master and the connector. |
| `market_cap`, `beta`, `pe_ratio`, `eps`, `avg_volume`, `forward_dividend`, `forward_dividend_yield`, `ex_dividend_date`, `earnings_date`, `target_est`, `dividend_announcement` | Quote-page fundamentals. Placeholders generated by the mock; neither report nor the price API supplies them. They need a separate reference-data source. |

---

## Fixed income: GFIM Trading Report

Seven sheets: a `SUMMARY` plus one sheet per section. Every security row has an
ISIN, which is the instrument's `symbol` in our master.

Our models are in `app/models/fixed_income.py`, one row type per section:
`GovernmentBondQuote` (New GoG, DDEP, Old GoG), `TreasuryBillQuote`,
`CorporateBondQuote` and `SellBuyBackQuote`, plus `FixedIncomeSummary` and a
`FixedIncomeReport` holding the lot. A blank report cell is `null`, never 0.
The models have no cross-field checks such as low ≤ close ≤ high, because real
data breaks them (quirks 7 and 9).

Served by `GET /fixed-income/report` (everything), `GET /fixed-income/summary`
and `GET /fixed-income/{section}` (`new_gog`, `ddep`, `old_gog`,
`treasury_bill`, `corporate`, `sell_buy_back`).

How the mock (`MockFixedIncomeMarket`) reproduces the report:

- Every active bill and bond in the instrument master starts from its closing
  values in the sample (the `mock` block of its seed entry). At the sample date,
  before any trades, the report matches the sample's rows and closing values.
- Securities trade at about their sample rate, so most rows on a given day have
  no volume. The 13 securities with no prices in the sample (five GFSF bonds,
  eight corporates) stay blank and never trade.
- Closing yields move only part-way toward each trade (end-of-day
  methodology), so they can close outside the day's traded range. Bonds that
  traded over a wide range in the sample keep trading over one.
- A day low/high carries over from the last session with trades, as in the
  sample.
- T-bills roll weekly. Bills are issued on Mondays for 91, 182 and 364 days,
  and matured ones drop out. A new issue has no opening price or yield on its
  first day. Bills issued after the sample aren't in the instrument master, so
  they get synthetic ISINs (`GHMK…`) and descriptions (`GOG-BL-…-MOCK-0`).
  Bills of different tenors maturing on the same date share one price.
- Bill prices are 100 / (1 + yield × days / 364), which reproduces the sample
  exactly. Bond prices use semi-annual clean pricing, with a per-bond offset
  calibrated from the sample for bonds (GFSF, USD DDE) whose convention differs.
- It does **not** reproduce the report's data-entry errors: shifted dates,
  reversed or junk ranges, prices in the yield column, stale typed-in numbers.
  Those are for the real connector to handle (see quirks).

### Instrument master: all sections

Seeded once into `data/instruments.json` from the sample (161 securities).

| Report field | `Instrument` field | Notes |
|---|---|---|
| ISIN | `symbol`, `isin` | Trailing spaces trimmed (quirk 10). |
| Security description | `name` | E.g. `GOG-BD-17/08/27-A6139-1838-10.00` is issuer-type-maturity-issue number-coupon. |
| Tenor | `tenor` | Only on a group's first row in some sheets (quirk 11); filled down. |
| Maturity date | `maturity_date` | Taken from the **description**, not the maturity column (quirk 6). |
| (coupon in description) | `coupon_rate` | 0 for bills; `None` for 3 corporates whose description has no coupon. |
| Issuers column (corporates) | `issuer` | `Government of Ghana` for GoG sections. |
| (sheet) | `segment` | `new_gog`, `ddep`, `old_gog`, `corporate`, `treasury_bill`. |
| (tenor prefix `USD-`) | `currency` | The four `USD-DDE-*` DDEP bonds are USD-denominated; the rest are GHS. |
| — | `asset_class` | `bill` for T-bills, `bond` for everything else (notes included). |

### Government notes and bonds: New GoG, DDEP, Old GoG sheets

| Report column | Our field | Status | Notes |
|---|---|---|---|
| No. | — | — | Row number within a group. |
| Tenor, Security description, ISIN, Maturity date | instrument master | ✓ | |
| Opening yield | `opening_yield` | ✓ | % a year. |
| Closing yield | `closing_yield` | ✓ | End-of-day methodology, so often outside the day's range (quirk 7). |
| End of day closing price | `closing_price` | ✓ | Per 100 face value. |
| Volume | `volume` | ✓ | Face value traded. Blank = no trades. |
| Number traded | `trade_count` | ✓ | Blank = no trades. |
| Day low yield / Day high yield | `day_low_yield` / `day_high_yield` | ✓ | Ranges can be very wide (2023-A-1: 11.99–15.59). Blank when nothing traded. |
| Days to maturity | `days_to_maturity` | ✓ | An Excel formula in the report; we compute it from the report date (quirk 8). |
| Applicable date | — | — | Mostly empty; the rest looks like leftovers (e.g. `1900-01-28`). |

The DDEP sheet has three groups: the 2023 DDEP bonds (`2023-A-1` … `2023-GC-12`),
the GFSF bonds (`GFSF-*`) and USD DDE bonds (`USD-DDE-*`).

### Treasury bills sheet

Grouped as `91-DAY BILL`, `182-DAY BILL` and `364-DAY BILL`, one row per
outstanding bill, with maturities one week apart. The newest bill in each group
has no opening price or yield (issued this week).

| Report column | Our field | Status | Notes |
|---|---|---|---|
| Opening price / Opening yield | `opening_price` / `opening_yield` | ✓ | Blank for the newest issue. |
| Closing price / Closing yield | `closing_price` / `closing_yield` | ✓ | Bills of different tenors that mature on the same date have identical open and close values (quirk 9). |
| Volume traded / Number traded | `volume` / `trade_count` | ✓ | |
| Day low price / Day high price | `day_low_price` / `day_high_price` | ✓ | Prices, not yields; often low > high, sometimes junk (quirk 9). |
| Days to maturity | `days_to_maturity` | ✓ | Computed from the report date (quirk 8). |
| Applicable date | — | — | Mostly empty; includes a stray `-46293`. |

### Corporate bonds sheet

Prices only; no yields. The footer says the end-of-day closing-price methodology
does **not** apply to corporates.

| Report column | Our field | Status | Notes |
|---|---|---|---|
| Issuers | instrument master `issuer` | ✓ | On the group's first row only. |
| Opening price / Closing price | `opening_price` / `closing_price` | ✓ | |
| Volume traded / Number traded | `volume` / `trade_count` | ✓ | No corporate trades on 28-Sep (total "-"). |
| Day low price / Day high price | `day_low_price` / `day_high_price` | ✓ | CMB-BD-30/08/27: low 100.87 > high 99.45 (quirk 9). |
| Days to maturity / Maturity date | `days_to_maturity` / `maturity_date` | ✓ | Computed from the report date (quirk 8); one cell (K6, 672) is a stale typed-in number. |
| Applicable date | — | — | As above. |

### Sell/buy-back trades sheet

A trade type on GoG bonds (New and DDEP), not a set of securities. Rows repeat
securities that are already in the master.

| Report column | Our field | Status | Notes |
|---|---|---|---|
| Group heading (`NEW BONDS`, `DDEP`, …) | `segment`, `tenor` | ✓ | |
| Yield | `yield_` (`yield` in JSON) | ✓ | For the USD DDE bonds this column holds the price (79.96, 86.80), not a yield (quirk 12). |
| Weighted average closing prices | `weighted_average_price` | ✓ | |
| Volume / Number traded | `volume` / `trade_count` | ✓ | |
| Days to maturity / Maturity date | `days_to_maturity` / `maturity_date` | ✓ | Computed from the report date; four cells in the sample are stale typed-in numbers (quirk 8). |

### SUMMARY sheet

| Report block | Our field | Status | Notes |
|---|---|---|---|
| Date heading (`Date: Monday, 28 September, 2026`, on every sheet) | `FixedIncomeReport.report_date`, `FixedIncomeSummary.report_date` | ✓ | Text, not an Excel date (quirk 15). |
| A. Grand totals: volume and number per section | `SectionSummary.volume` / `.trade_count`; grand total `FixedIncomeSummary.total_volume` / `.total_trade_count` | ✓ | Links to each sheet's `TOTAL` row, which is `=SUM(...)` (quirk 8). The grand total is computed. |
| B. Largest volume traded: per section volume, number, security, yield, closing price | `SectionSummary.largest_trade` | ✓ | Derivable from the section rows; the report repeats it. |

---

## Quirks

Things the real connector must handle. Each one is visible in the samples.

1. **The equities report lists 25 securities, not all listed ones.** The source
   workbook itself has only 25 rows (A1:M26), from ACCESS to RBGH. SCB, SIC,
   SOGEGH, TOTAL, UNIL, ZEN and others are missing. The PDF isn't truncated.
   *Open question for the GSE: does the full report cover every listed name?*

2. **Status markers in share codes.** `**ALW**` and `PBC**` carry asterisks with
   no legend. We assume they mean **suspended**; both are `status: "suspended"`
   in the master and not streamed. *Confirm with the GSE.* Strip the markers
   before using the code as a symbol.

3. **The official closing price is a VWAP, not the last trade.** Both the
   previous-close and closing columns are VWAPs; "Last Transaction Price" is
   the last trade. With no trades, the closing VWAP carries over the
   previous one. "Price Change" is between the two VWAPs.

4. **Many equities don't trade.** 6 of 25 (AGA, ALW, ASG, CMLT, MAC, PBC) had
   zero volume on 28-Sep. Their open, last and close all equal the previous close.

5. **One-sided and crossed-looking books.** Most names close with only a bid or
   only an offer, and some with neither (ALW, PBC). Resting orders can sit on
   the "wrong" side of the last price: AGA bid 40.70 vs close 37.00, CPC bid
   0.28 vs 0.26, CMLT bid 0.15 vs 0.14, and CLYD offer 4.01 vs 4.02. The last price is stale, so don't
   validate bid/ask against it.

6. **GFIM maturity column disagrees with the description for five GFSF
   bonds.** For GFSF-7-5YR, -10-11YR, -9-12YR, -4-13YR and -5-14YR the column
   holds the wrong date. Rows 29–31 hold the *next* row's date, row 32 holds
   row 25's, and row 25's has the wrong year (2027 instead of 2028). The
   descriptions agree with the tenor labels (GFSF issued Nov/Dec 2023 + N
   years), so we use the description. Same error in the sell/buy-back sheet.

7. **Government closing yields are end-of-day prices, not trades.** They often
   fall outside the day's traded low–high (2023-GC-3 closes at 13.63 against
   12.87–12.92; the 15-year GOG-BD-10/07/34 at 28.11 against 19.97). Don't
   require `day_low ≤ close ≤ day_high` for fixed income.

8. **Totals and days to maturity are Excel formulas, not values.**
   - Totals are `=SUM(...)`, and the SUMMARY sheet references the other sheets.
   - Days to maturity is `=maturity − TODAY()`, so it depends on when the file
     was opened. The workbook shows 7 days for the 05-Oct-26 bills (as of
     28-Sep), but the PDF shows 6, so it was evidently produced a day later.
   - Five days-to-maturity cells are typed-in numbers, stale by 140–172 days:
     sell/buy-back I6, I9, I10, I11 (2546, 495, 859, 495) and corporate K6 (672).
   
   Always compute days to maturity from the report date and our maturity date,
   and compute totals from the rows.

9. **Price ranges are unreliable.** In the T-bill and corporate sheets, "day
   low price" is often above "day high price" (typically low = 100.0000). There
   are junk highs: 182-day #2 has 0.0967 and 364-day #21 has 0.1053. Bills of
   different tenors maturing on the same date show identical open and close
   values, which looks priced off one curve. Treat low/high as unordered and
   drop impossible values.

10. **Whitespace and typos in identifiers.** Two bill ISINs have a trailing
    space (`GHGGOGI02055 `, `GHGGOGI02154 `). One corporate description uses
    the letter O for a zero (`BFS-BD-17/10/26-CO859-23.50`). The issuer
    "IZWE SAVINVGS" is misspelled. Sheet names have trailing spaces
    (`'CORPORATE  '`). Trim everything, and match on ISIN, not description.

11. **Grouping only on the first row.** Tenors (old GoG, T-bills) and issuers
    (corporates) appear only on a group's first row, and Bayport's rows are
    numbered from 2. Fill values down.

12. **The USD sell/buy-back "yield" is a price.** For USD-DDE-FCA-28 and
    -FEA-28 the yield column equals the weighted average price (79.96, 86.80).

13. **Inconsistent description formats.** Corporate maturity dates in the
    description use 2- or 4-digit years (`PPE-BD-14/08/2030-16.25`), some
    descriptions have no coupon (`LGH-BD-04/10/29-C0936`), and Cocobod's use
    the government format (`CMB-BD-…-A6302-1675-13.00`).

14. **Template leftovers.** The T-bill sheet has a stray `DATE: MAY 07, 2021`
    header next to the real date, and the "Applicable date" columns hold junk
    (`1900-01-28`, `-46293`, dates in 2019–2020). Ignore both.

15. **The date is text.** GFIM sheets carry it in a heading cell
    (`Date: Monday, 28 September, 2026`); the equities report repeats it as
    `DD/MM/YYYY` text on every row. Neither is an Excel date.

## Open questions for the GSE

- What do the `**` markers mean (quirk 2)? We currently treat them as suspended.
- Does the full equities report list every listed security (quirk 1)?
- Are "Year High/Low" for the calendar year to date or a trailing 52 weeks?
- What does "Applicable date" mean, and should it be populated?
- The GFIM notes say "securities coloured red are benchmark securities". Colour
  isn't data; will the API flag benchmarks explicitly?
