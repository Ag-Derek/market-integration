# GCX spot contracts

The Ghana Commodity Exchange (GCX) trades spot contracts in grain and
oilseeds: white and yellow maize, soya bean, sesame, sorghum and rice.
They are a fourth asset class here, `commodity`, beside equities, bills
and bonds. They stream through the same pipeline: ticks, candles,
search, the market strip, the Graph tab and trade tickets.

The feed is **simulated**. It is modelled on what GCX publishes, as the
GSE and GFIM mocks are (see [data-formats.md](data-formats.md)). For a
real feed, see [A live source](#a-live-source).

The code:

- `app/models/instrument.py`: the `commodity` asset class and contract terms;
- `app/models/commodity.py`: `CommodityTick`;
- `app/connectors/gcx_mock.py`: the mock;
- `app/commodities/gcx.py`: the world-benchmark comparison.

The monthly world reference prices on the same tab are a separate
feature: see [commodity-prices.md](commodity-prices.md).

## Sources

What was checked, and when:

| Source | What it gave |
|---|---|
| GCX market reports (maize), [10-14 Jun 2019](https://dev.gcx.com.gh/gcxadmin/uploads/file/new-weekly-report-10th-14th-june-2019.pdf) and [5-9 Aug 2019](https://www.dev.gcx.com.gh/gcxadmin/uploads/file/5th-9th-aug-2019.pdf) | The report's columns, symbols (GEJYM3, GWAWM3, GWAWM2, GSAWM1, GWEWM2) and their key: warehouse, crop and grade. Prices in GH₵ per tonne, volumes in tonnes to 4 decimals. |
| gcx.com.gh, read 23 Oct 2026 | The site's crop codes (WM, YM, SB, SS, SG, SH; R for rice), a sample contract list (GAPWM2, GAPYM2, GKUYM2, GSRSM2, GARRC2, GSESM2), a table of prices by delivery centre in GH₵ per tonne (undated), trading hours, and T+1 settlement. |
| [GCX on X](https://x.com/gcx_official), Oct 2026 | Daily closing prices for yellow maize per 50 kg bag at Tamale, Sandema, Tumu and Wa. GCX's public posts quote per bag, so the UI shows a per-bag price too. |

## The market report

A GCX market report has one row per contract that traded. Each column
maps onto `CommodityTick` as follows:

| Report column | Our field | Notes |
|---|---|---|
| Symbols traded | `symbol` | GCX's code; see [Symbols](#symbols). |
| Opening price | `open` | The session's first trade. Shown as `-` when there was none, so the field is `None` until the contract trades. |
| Closing price | `price` | The last trade. On the GSE the closing price is a VWAP; on GCX it isn't, so the close carries into the next session's `previous_close`. |
| Price change (%) | `change` | Stored as GH₵, `price − previous_close`. The UI works out the percentage. |
| High, Low | `day_high`, `day_low` | `None` until the contract trades. |
| Volumes traded (MT) | `volume` | Stored in **kilograms**: candles keep volume as an integer, and the report gives tonnes to 4 decimals (10.7826 MT). Kilograms keep three of those decimals. |

Fields the report doesn't have, expected from a live feed: `bid` and
`ask` with their sizes in kilograms, `trade_count`, `value_traded` in
GH₵, `previous_close`, the 52-week range, and the two clocks
`timestamp` and `last_trade_at`, as for equities.

## Units

- **Prices** are cedis per metric tonne, as GCX quotes them.
- **The 50 kg bag** is a twentieth of a tonne. `/gcx/contracts` and the
  pages show the bag price beside the tonne price, because GCX's own
  posts quote per bag.
- **Volumes** are kilograms in ticks and candles, and tonnes in the UI
  and on tickets.
- **Ticket quantities** are tonnes to 4 decimals, as GCX reports them.

## Symbols

A symbol is `G`, then a two-letter warehouse code, then a two-letter
crop code, then the grade. GEJWM2 is white maize, grade 2, delivered to
the Ejura warehouse.

**Crops:** WM white maize, YM yellow maize, SB soya bean, SS sesame,
SG sorghum, RC rice. These come from the site's decoder, except rice:
the decoder matches on "R", and the contract list has GARRC2, so we use
RC.

**Warehouses** seen in GCX material: EJ Ejura, WA Wa, SA Sandema,
WE Wenchi, KU Kumasi, AP Afram Plains, SR Saboba.

**Inferred, not seen:** TA for Tamale and BO for Bolgatanga.

The site's sample contract list isn't fully consistent. It labels
GKUYM2 "Yellow Soya Bean", although YM is yellow maize everywhere
else, and gives both sorghum and sesame as SM. The seed follows the
reports and the decoder instead.

## The contracts seeded

17 contracts in `data/instruments.json`, one line each. Each `mock`
block's `source` says where its price and its symbol come from:

| Crop | Contracts |
|---|---|
| White maize | GEJWM2 Ejura, GWEWM2 Wenchi, GWAWM2 Wa, GSAWM1 Sandema (grade 1), GKUWM2 Kumasi |
| Yellow maize | GEJYM3 Ejura (grade 3), GKUYM2 Kumasi, GTAYM2 Tamale, GWAYM2 Wa |
| Soya bean | GTASB2 Tamale, GWASB2 Wa, GKUSB2 Kumasi |
| Sesame | GTASS2 Tamale, GSASS2 Sandema |
| Sorghum | GWASG2 Wa, GSASG2 Sandema |
| Rice | GBORC2 Bolgatanga |

Starting prices come from the site's delivery-centre table, for example
white maize at Ejura GH₵ 3,000/MT and sesame at Tamale GH₵ 16,500/MT.
That table is undated: GCX's October 2026 maize posts put Wa about
GH₵ 1,400/MT above it. Trading activity and book shapes are assumed.

Adding a contract is one line in the seed, as for any instrument. Its
`mock` block takes the GSE mock's fields (`app/connectors/gse_mock_profiles.py`).
For commodities, `avg_volume` is kilograms a day and `spread` is cedis
per tonne.

## Session and settlement

**Hours:** 09:00-15:00 GMT on GSE trading days (`GCX_SESSION_OPEN`,
`GCX_SESSION_CLOSE`). That is what gcx.com.gh gives on its contract
pages. Its home page says "8AM - 4PM GMT", so confirm with GCX.

**Status:** `/market/status` and the WebSocket status carry the GCX
session under `commodities`, beside `fixed_income`.

**Settlement:** T+1 (`COMMODITY_SETTLEMENT_LAG`). GCX says "sellers
are paid and buyers receive full delivery within 24 hours after trade".

## The mock

`MockCommodityConnector` is the equity mock (`MockMarketConnector`)
with a GCX quote on top. It reuses the random walk, the ten years of
backfilled history, the trading tiers and the book shapes. Like the
equity mock, it compresses activity for a demo: the maize contracts
trade about once a minute, each trade small.

## Against the world price

`/gcx/contracts` compares each contract with the World Bank Pink Sheet
series for its crop (`app/commodities/gcx.py`). Both prices are in
cedis per tonne. The benchmark is put into cedis at its month's average
Bank of Ghana USD mid, the same way the Commodities tab does.

| GCX crop | Benchmark |
|---|---|
| White and yellow maize | `MAIZE`: US no. 2 yellow, f.o.b. Gulf |
| Soya bean | `SOYBEANS`: added to the offered series for this |
| Rice | `RICE`: Thai 5% broken, f.o.b. Bangkok |
| Sesame | None: the Pink Sheet has no sesame series |
| Sorghum | None: the Pink Sheet's series ends in August 2020 |

`basis_percent` is the GCX price's premium (+) or discount (−) to the
benchmark. Treat it as indicative. A benchmark is a monthly export
price at a foreign port, while a GCX price is today's spot price for a
grade delivered to a Ghanaian warehouse. The gap mixes quality,
transport and the cedi with the price itself.

## API

- **`GET /gcx/contracts`** lists every contract, by crop, with:
  - its terms;
  - its latest quote, in GH₵ per tonne and per bag;
  - the session so far;
  - its benchmark.
- **`GET /gcx/contracts/{symbol}`** returns one contract, or 404.
- **`GET /commodity/{symbol}`** is the contract page.
- **`/candles`**, **`/graph/series`** (kind `spot`), **`/search`**,
  **`/market/{symbol}`** and **`/ws/market`** take GCX symbols like any
  other.
- **`/candles?currency=`** converts commodity prices too, since they
  are in cedis.
- **`/tickets`** accepts GCX contracts: quantity in tonnes, price per
  tonne, no accrued interest, settling T+1.

- **`GET`/`PUT /gcx/watchlist/me`** is the user's pinned contracts, in
  pin order. It's a list of its own, in the `commodity_watchlists`
  table, with the same rules and pin limit as the ticker's equity
  watchlist. It starts empty.

Pinned contracts sit in a "Pinned" group at the top of the Commodities
tab's GCX table. They're pinned with the star on each row, or the Pin
button on a contract's page. The ticker's watchlist stays equities
only.

## A live source

GCX doesn't offer a documented market-data API that we know of. Its
website reads its prices from a Firebase Realtime Database:
`closing_prices.json` and `commodity_symbols.json`, under the site's
`marketdata` path. They give each contract's opening, closing, high
and low prices, price change and last trade date, plus its delivery
centre and grade.

That database is the website's backend, not a published feed. Don't
build on it without GCX's agreement: ask GCX for data access, and for
the terms of use, before writing a connector against it.

With access agreed, a real connector implements `BaseMarketConnector`
(`app/connectors/base_connector.py`):

- `normalize()` maps GCX's fields onto `CommodityTick` using the table
  in [The market report](#the-market-report);
- `fetch_history()` serves the daily closes.

Nothing downstream changes.

## Open questions for GCX

- **Warehouse codes:** the codes for Tamale and Bolgatanga, and the
  full list of current contracts.
- **Grades:** which grades are listed for each crop outside maize.
- **Trading hours:** 09:00-15:00 or 08:00-16:00.
- **Increments:** the price tick size, the minimum lot, and whether
  quantities trade in whole bags.
- **Bid and offer:** whether the trading platform publishes them.
