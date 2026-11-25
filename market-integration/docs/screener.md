# Stock screener (EQS)

The Screener tab (`/screener`, `app/static/screener.html`) filters and
sorts GSE companies by their ratios, like Bloomberg's EQS. Its data is
`GET /screener/results`; the logic is `app/screener/` and the response
model `app/models/screener.py`.

## Universe

Every company with a file in `data/companies/`, whether or not its
financials are filled in yet. With no filters set, every one appears.
Name and sector come from the instrument master (the same sectors as
`/sectors` and the search bar's Sector filter).

## Figures

Each figure is the one the company's Description page shows: the
screener calls `calculated_figures()` in `app/company/figures.py`, so the
two can't disagree. Every figure carries its formula, shown on hover.

| Column | Formula | Unit |
|---|---|---|
| Price | session VWAP (the GSE closing price); the latest daily close with no live quote | GHS |
| Market cap | price x shares outstanding | GHS |
| P/E | price / trailing EPS (latest fiscal year with EPS) | x |
| P/B | market cap / book value (total shareholders' equity) | x |
| Dividend yield | latest dividend per share / price x 100 | % |
| ROE | net income / book value x 100 (latest year with both) | % |

Ratios are never taken between currencies: a company reporting in USD,
AUD or GMD has null ratios ("eps only reported in another currency").

## Filters

| Parameter | Keeps | Unit |
|---|---|---|
| `sector` | that sector (case-insensitive; 400 if no company is in it) | |
| `pe_max` | P/E ≤ value | x |
| `pb_max` | P/B ≤ value | x |
| `yield_min` | dividend yield ≥ value | % (`5` is 5%) |
| `roe_min` | ROE ≥ value | % |
| `market_cap_min` | market cap ≥ value | GHS, in full (the page's box is in GHS millions and converts) |

Bounds are inclusive. Filters combine (a row must pass all of them).

## Null rules

- **A company without a filtered figure is left out, never treated as
  zero.** A loss-maker has no P/E rather than a P/E of 0 that would pass
  `pe_max`; a company with no recorded dividend has no yield rather than
  0% (so `yield_min=0` keeps only companies with a recorded dividend).
- **Left-out companies are counted per filter,** in the response's
  `filters`: `left_out`, the `symbols`, the figure's `reasons` with a
  count each, and a sentence for the page ("1 company was left out
  because it has negative earnings."). Counts are taken over the
  universe after the sector filter, and each filter counts on its own: a
  company missing both P/E and yield counts under both.
- Companies that have the figure but fail the bound aren't "left out";
  they just don't match.
- **Sorting never drops a row.** Rows without the sort figure go last,
  in symbol order.

The reasons are figures.py's: `no price` (no live quote and no stored
close, e.g. a suspended stock), `no eps recorded`, `negative earnings`,
`negative book value`, `… only reported in another currency`, `no
shares outstanding recorded`, `no year with both net income and book
value`.

## Sort

`sort` is one of `dividend_yield` (default, highest first), `pe_ratio`
(lowest first), `price_to_book` (lowest first), `return_on_equity`
(highest first), `market_cap` (largest first) or `symbol`. Ties are in
symbol order. DCF upside joins once the DCF panel exists.

## The page

The page keeps its filters in its own address
(`/screener?pe_max=12&yield_min=4&sort=pe_ratio`), so a screen can be
bookmarked or shared, and re-runs the screen every minute while visible.
Each symbol links to its Description page.
