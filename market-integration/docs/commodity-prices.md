# Commodity prices

The Commodities tab (`/commodities`) shows monthly reference prices for the
world benchmarks that matter to Ghana's economy: crude oil, gold, cocoa and
the other exports, the staples and fertiliser it imports, and the metals its
mining sector trades against. Prices are in US dollars as published, and in
cedis at the Bank of Ghana's USD fixes. The code is in `app/commodities/`.

These are reference prices, like the FX fixes, not tradable instruments.
Nothing here goes through the tick pipeline, candles or tickets. The
tradable commodities on the same tab, Ghana Commodity Exchange spot
contracts, are in [commodities-gcx.md](commodities-gcx.md); they are
compared with the maize, soybean and rice series here.

## Source: the World Bank Pink Sheet

[World Bank Commodity Price Data (The Pink Sheet)](https://www.worldbank.org/en/research/commodity-markets)
publishes monthly average prices for about 70 commodities, back to January
1960, in nominal US dollars. It is free, needs no key, and is updated in the
first week of each month with the month before (the October 2026 release came
out on 2 October and runs to September 2026). Earlier months can be revised
in any release.

It comes as one Excel workbook, `CMO-Historical-Data-Monthly.xlsx`, of about
600 KB. Its address carries a document id that changes from year to year:

    https://thedocs.worldbank.org/en/doc/74e8be41ceb20fa0da750cda2f6b9e4e-0050012026/related/CMO-Historical-Data-Monthly.xlsx

So each pull first reads the current link off the Commodity Markets page
(`worldbank.monthly_url`), then downloads the workbook. We read its
**Monthly Prices** sheet:

| Row  | Column A                        | Columns B onwards                                     |
|------|---------------------------------|-------------------------------------------------------|
| 4    | `Updated on October 02, 2026`   |                                                       |
| 5    |                                 | series names: `Crude oil, Brent`, `Cocoa`, `Gold`, ...|
| 6    |                                 | units in brackets: `($/bbl)`, `($/kg)`, `($/troy oz)` |
| 7... | the month: `1960M01` ... `2026M09` | the month's average price                          |

The parser finds these rows by their content, not by fixed row numbers: the
first row whose column A is a `YYYYMmm` month starts the data, the units row
is the nearest above it whose cells are all bracketed, and the names row is
the nearest above that. Some other details:

- **Missing months.** A month a series wasn't priced is `…` or `...`, and is
  skipped. A few old cells hold spreadsheet errors (`#VALUE!`) or `0`. These
  are skipped too, with one warning line per pull.
- **Footnote marks.** Some names end in `**`, which marks a series that
  replaced another in a World Bank price index (for example, Rubber TSR20
  replaced RSS3 in 2018). It says nothing about the price, so it is dropped
  from the name. Trailing spaces (`Urea `) are dropped too.
- **Units.** Units are stored as published, without the brackets.
- **No new dependency.** An `.xlsx` is a zip of XML, so the workbook is read
  with `zipfile` and `xml.etree` from the standard library.

The workbook's **Description** sheet defines each series. For the ones we
offer, in short:

- **Brent:** UK Brent 38° API.
- **Gold:** spot average of daily rates from June 2025; before that, the
  London afternoon fixing.
- **Cocoa:** the ICCO daily price, the average of the first three positions on
  New York and London.
- **Maize:** US no. 2 yellow, f.o.b. Gulf.
- **Urea:** prill, f.o.b. Middle East.

These are international indicative prices, not the prices Ghana realises.
Cocoa, for example, is sold forward by COCOBOD.

## What is offered

Every series in the workbook is stored. `COMMODITIES` in
`app/commodities/__init__.py` picks the ones offered:

| Code        | Series (World Bank)  | Unit          | Group       |
|-------------|----------------------|---------------|-------------|
| `BRENT`     | Crude oil, Brent     | $/bbl         | Energy      |
| `WTI`       | Crude oil, WTI       | $/bbl         | Energy      |
| `GOLD`      | Gold                 | $/troy oz     | Metals      |
| `SILVER`    | Silver               | $/troy oz     | Metals      |
| `ALUMINIUM` | Aluminum             | $/mt          | Metals      |
| `COPPER`    | Copper               | $/mt          | Metals      |
| `COCOA`     | Cocoa                | $/kg          | Agriculture |
| `COFFEE`    | Coffee, Robusta      | $/kg          | Agriculture |
| `PALM_OIL`  | Palm oil             | $/mt          | Agriculture |
| `RUBBER`    | Rubber, TSR20        | $/kg          | Agriculture |
| `COTTON`    | Cotton, A Index      | $/kg          | Agriculture |
| `MAIZE`     | Maize                | $/mt          | Agriculture |
| `SOYBEANS`  | Soybeans             | $/mt          | Agriculture |
| `RICE`      | Rice, Thai 5%        | $/mt          | Agriculture |
| `WHEAT`     | Wheat, US HRW        | $/mt          | Agriculture |
| `SUGAR`     | Sugar, world         | $/kg          | Agriculture |
| `UREA`      | Urea                 | $/mt          | Fertiliser  |

Prices are shown in the World Bank's units, so cocoa is per **kg** (US$ 5.92
in September 2026), not per tonne as futures markets quote it. Multiply by
1,000 for a per-tonne figure.

**Unit guard.** Each commodity names the unit it expects. If the source
publishes the series in a different unit, the commodity is dropped, not
shown: cocoa switching from $/kg to $/mt would otherwise be wrong by a factor
of 1,000. A dropped commodity is listed in `/health` under
`commodities.unit_mismatches` until `COMMODITIES` is updated.

**Offering another series** is one line in `COMMODITIES`. The data is already
stored, so no new pull is needed. Copy the series name and unit exactly from
row 5 and row 6 of the workbook, minus any `**`, brackets and trailing
spaces.

## In cedis

A month's cedi price is its US dollar average times that month's average of
the Bank of Ghana USD mid fixes, from `app/fx/`. Both are averages over the
same month. For example, September 2026 gold was US$ 4,319 × GH₵ 11.5012
(the mean of 21 fixes) = GH₵ 49,673.66 per troy oz.

Where no fixes are held for a month, `ghs` is null. That happens before the
FX history, which reaches back about 11 years, or before BoG has been
reached at all. `ghs_yoy_percent` compares cedi prices a year apart, so it
includes the cedi's own move as well as the commodity's.

## Storage and refresh

Prices are stored in the `commodity_prices` table in `MARKET_DB_PATH`. There
is one row per source, series and month, with the unit and price as
published. Like the FX fixes, the table is kept across restarts and never
rebuilt. Each pull upserts the whole release (about 50,000 prices, a couple
of seconds), so a revised month replaces the old price but keeps
`fetched_at`, the time we first saw it.

The service pulls at startup and then every `COMMODITY_REFRESH_SECONDS`
(default 86400, daily). After a failure it retries after 30 minutes. A failure
never removes stored prices. It is logged and shown in `/health` under
`commodities.sources.worldbank.last_error`. Set the setting to 0 to never pull
(tests do).

## API

- **`GET /commodities/prices`** returns each commodity's latest month.
  `?month=YYYY-MM` gives the month in force then instead (a series that
  skipped that month shows its month before). Each row has:
  - `usd` and `unit`;
  - the month before (`previous_month`, `previous_usd`, `change_percent`);
  - the year before (`year_ago_month`, `year_ago_usd`, `yoy_percent`);
  - `ghs`, `ghs_per_usd`, `fx_fixes` and `ghs_yoy_percent`.

  Errors: 503 until the first pull, 404 for a month before the history, 400
  for a malformed month.
- **`GET /commodities/{code}/history?since=YYYY-MM`** returns monthly points
  `{month, usd, ghs}`, oldest first. Errors: 400 for a code not offered, 503
  for one offered but not yet held.
- **`GET /graph/series?series=CMD:GOLD`** charts a commodity on the Graph
  tab: one point a month, dated the 1st, in US dollars, rebased like any
  other series. The page's picker offers commodities, and each name on
  `/commodities` links to its 5-year graph. Correlation against daily series
  only uses the days both have a value, so it rarely reaches the
  20-observation minimum. Use weekly frequency, or compare commodities with
  each other.

## Adding the Bank of Ghana as a second source

BoG publishes
[Monthly Commodity Prices](https://www.bog.gov.gh/economic-data/commodity-prices/):

- Brent (US$/barrel);
- gold (US$/fine ounce);
- cocoa (US$/tonne);
- indicative petrol and diesel prices in cedis per litre, set by the NPA.

The store and lookups are keyed by source to make room for it:

1. Add `"bog"` to `SOURCES` in `app/commodities/__init__.py`.
2. Add `app/commodities/bog.py`: a client with `source = "bog"` and a
   `fetch()` that returns a `Release`. If the page is a wpDataTables table,
   as the FX and T-bill pages are, `app/bog.py`'s `WpDataTable` reads it.
3. Pass the client to `CommodityPrices` in `app/main.py`, after
   `WorldBankClient()`.
4. In `COMMODITIES`, name the BoG series for each commodity it covers. The
   first source listed that holds prices is used. Add the pump prices as new
   commodities: they are in cedis, so they will need a currency field rather
   than the US dollar assumption.

On 23 October 2026, when this was built, BoG's site was resetting every
connection (the FX pull, which succeeded on 21 October, included), so the
table's layout couldn't be checked. Look at it before writing the parser.
