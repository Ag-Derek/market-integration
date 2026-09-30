# Ghana fixed income: sources and conventions

Decision doc for the spike in issue #33. It covers where fixed income data will
come from, what each source gives us, and the conventions the bond math library
(#36) implements.

Field-by-field mapping of the GFIM daily report is in
[data-formats.md](data-formats.md); this doc doesn't repeat it.

## Decisions

1. **Primary source: GFIM end-of-day data from the GSE.** Until the GSE API docs
   arrive (#15), that means the GFIM daily trading report. The mock already
   reproduces it. Assume end of day, not intraday, until the API says
   otherwise.
2. **Secondary source: Bank of Ghana weekly T-bill auction rates**, for the
   primary-market reference rate and history. We can get them programmatically
   (see [below](#bank-of-ghana-t-bill-auction-results)), but only from an
   undocumented endpoint, so treat it as best-effort.
3. **Quote basis follows GFIM Rule 14.** Government notes, bonds and bills are
   quoted by **yield**; we derive price from it. Corporates are quoted by
   **price**, because that is all the report gives.
4. **Bond math v1 covers:** T-bills, New GoG bonds, the 2023 DDEP bonds
   (`2023-A-*`, `2023-B-*`, `2023-GC-*`) and Old GoG bonds. These are fixed-coupon
   semi-annual bullets, and the conventions below reproduce the report.
5. **Out of bond math v1, but still displayed as reported:** the GFSF bonds, the
   four USD DDE bonds and corporate bonds. We show their reported yield and/or
   price, without computed analytics, until we have term sheets (see
   [below](#instruments-the-conventions-dont-fit)).
6. **Corporate bonds are display-only in v1.** Reasons: the report has prices
   but no yields; the closing-price methodology explicitly excludes them; three
   descriptions have no coupon; nothing traded on the sample day; and we have
   no term sheets.
7. **Licensing is still open, and it blocks production.** It is not blocking
   development. See [licensing](#licensing-and-redistribution).

## Answers to the spike's questions

| Question | Answer | Confidence |
|---|---|---|
| GSE API coverage of GFIM bonds and bills, and update rate | **Unknown until the docs arrive (#15).** The GSE's published price list (Apr 2024) lists equity and index products only; it has no GFIM line. | Open |
| T-bill auction results programmatically? | **Yes.** BoG's T-bill rates page is backed by a JSON endpoint with the full history (2,011 rows). It is undocumented and needs a page nonce. | Verified 30-Sep-2026 |
| T-bill secondary pricing? | **Yes, in GFIM:** every outstanding bill, with open/close price and yield, volume and day range (239 trades on 28-Sep). | Verified on the sample |
| GoG bond secondary data from GFIM? | **Yes, end of day:** opening and closing yield, closing price, day range, volume and trade count, per ISIN. | Verified on the sample |
| Which bonds are current after the DDE? | New GoG (2 on 28-Sep), 2023 DDEP A/B/GC (16), GFSF (9), USD DDE (4), and Old GoG still outstanding (17). All 161 securities are in `data/instruments.json`. | Verified on the sample |
| Corporate bonds in v1? | Display only (decision 6). | Decision |
| Price or yield? Decimal? | Yield for government securities, with clean price shown (Rule 14); price for corporates. **Prices are decimal to 4 dp (Rule 23), not 32nds.** | GFIM Rules 2022 |
| Day count, frequency, settlement lag, discount vs interest rate | See the [conventions table](#conventions-table). | Mixed; see the table |
| Licensing and redistribution | Open. See [below](#licensing-and-redistribution). | Open |

## Sources

### GFIM (GSE): the primary source

- **What:** secondary trading in all GoG, BoG and Cocobod securities, including
  T-bills, plus admitted corporates. Trades settle through the CSD, with cash at
  the BoG.
- **Hours:** 09:00–16:00 GMT on business days. Orders are mass-suspended at
  16:00 (Rule 12). This is the session the fixed income calendar should use; it
  differs from equities.
- **Trading:** RFQ, firm orders or negotiated trades that are then reported.
  The standard RFQ amount in benchmark securities is GH¢500,000, and the maximum
  spread is 50bp for standard lots (Rules 13, 16, 17).
- **Daily report:** seven sheets (see [data-formats.md](data-formats.md)).
  gfim.com.gh has a "Daily Trading Reports" page with tabs for 2023–2026. The
  file links load dynamically and I didn't confirm a stable URL pattern. We
  don't plan to scrape it (the report is a one-time reference, per
  data-formats.md), but it is the fallback for history if the API has none.
- **API:** unknown (#15). Questions specific to fixed income are
  [listed below](#questions-for-the-gse-add-to-15).

### Bank of Ghana T-bill auction results

- **Page:** `https://www.bog.gov.gh/treasury-and-the-markets/treasury-bill-rates/`
- **Data:** issue date, tender number, tenor (91/182/364 day), **discount
  rate** and **interest rate**, one row per tenor per week, back to the start
  of the series.
- **How to get it:** the table is a wpDataTables server-side table. `POST
  /wp-admin/admin-ajax.php?action=get_wdtable&table_id=2` with DataTables
  paging parameters (`draw`, `start`, `length`, `order[0][…]`) and `wdtNonce`
  returns JSON. The nonce is in the page's hidden input
  `wdtNonceFrontendServerSide_2`, so fetch the page first. Sample response
  (30-Sep-2026):

  ```json
  {"recordsTotal":"2011","data":[
    ["28 Sep 2026","2026","182 DAY BILL","6.1734","6.3700"],
    ["28 Sep 2026","2026","364 DAY BILL","8.9534","9.8339"],
    ["28 Sep 2026","2026","91 DAY BILL","4.6244","4.6785"], ...]}
  ```

- **Linking to GFIM:** the tender number is the fourth field of a bill's
  description. Tender 2026, issued 28-Sep, is `GOG-BL-28/12/26-A7125-2026-0`
  (91-day), `GOG-BL-29/03/27-A7129-2026-0` (182-day) and
  `GOG-BL-27/09/27-A7144-2026-0` (364-day), which are the newest bill in each
  GFIM group. So an auction row maps to an ISIN by tenor and tender number.
- **Timing:** auctions are on Fridays, and bills are issued the following
  Monday. The mock's Monday issue is correct.
- **Limits:** it gives rates only. Amounts offered, bid and accepted appear in
  BoG press releases, not in this table. The endpoint is undocumented and could
  change without notice, so poll it daily and alert on failure.
- **Not used for:** secondary prices. The GFIM close on the new 91-day bill
  (4.70425%) is not the auction rate (4.6785%).

### Not pursued

- **Commercial vendors** (Bloomberg, Refinitiv, cbonds). They cover GoG ISINs
  (cbonds lists 2023-GC-2, for example), but they redistribute the same GFIM
  data under more restrictive terms. cbonds blocked automated access.
- **Dealer quote pages.** SEC guidelines require dealers to publish daily
  two-way quotes on their websites. They are indicative, inconsistent in format
  and per dealer, so they're not a feed.

## Conventions table

This is what #36 implements. "Verified" means the formula reproduces the
28-Sep-2026 report. The fit is in
[How the conventions were checked](#how-the-conventions-were-checked).

| | T-bills (91/182/364d) | GoG notes and bonds (New, 2023 DDEP, Old) | Corporate bonds |
|---|---|---|---|
| **Quote basis** | Yield (Rule 14); the report also gives price | Yield (Rule 14), with clean price displayed | Price (report has no yields) |
| **Price format** | Decimal per 100 face, 4 dp (Rule 23) | Decimal per 100 face, 4 dp (Rule 23) | Decimal per 100 face |
| **Yield type** | **Interest rate (simple yield), not discount rate** | Yield to maturity, semi-annual compounding | n/a in v1 |
| **Price ↔ yield** | `P = 100 / (1 + y·d/364)` | Standard bullet: PV of semi-annual coupons + redemption at `y/2` per period, fractional first period; clean = dirty − accrued | n/a in v1 |
| **Day count** | **ACT/364** | **ACT/ACT (ICMA)** for the period fraction and accrued interest. ACT/364 and ACT/365 fit almost as well; confirm with the GSE | Unknown; needs term sheets |
| **Coupon frequency** | None (zero coupon) | **Semi-annual**, on the maturity date's day and month, stepping back 6 months | Unknown; assume semi-annual |
| **Coupon rate** | 0 | From the description's last field (e.g. `…-8.35`) | From the description where present; 3 have none |
| **Days / time basis** | Days from the **trade (report) date** to maturity | Trade (report) date (fits marginally better than T+2) | — |
| **Settlement lag** | T+2 (Rule 28); T+0/T+1 by bilateral agreement | T+2 | T+2 |
| **Discount ↔ interest** | `y = dr / (1 − dr·d/364)` (BoG publishes both) | — | — |
| **Business-day rule** | Unconfirmed; maturities fall on Mondays | Unconfirmed; coupons are assumed to be paid on the unadjusted date | — |
| **Currency** | GHS | GHS; the 4 USD DDE bonds are USD (out of v1 math) | GHS |

Notes for #36:

- **Report prices use the trade date, not settlement.** Bill prices reproduce
  exactly with days from the report date, and are off by up to 0.026 with T+1.
  The report's "end of day closing price" is therefore a valuation price.
  Invoice amounts for a real trade should use the T+2 settlement date. Make
  the valuation date a parameter.
- **Discount rate vs interest rate.** GFIM quotes the interest rate. BoG
  publishes both, related on 364 days: `4.6244 / (1 − 0.046244·91/364) =
  4.6785`, which matches BoG's published 91-day rate exactly. Pricing a bill as
  `100·(1 − y·d/364)` with the GFIM yield is wrong by up to 0.87.
- **Yield is primary for government securities.** Where the report's closing
  price disagrees with its closing yield (common in Old GoG; see below),
  display the reported price but derive analytics from the yield.
- **The final coupon period is compounded, like every other period.**
  Excel's PRICE switches to simple interest when one coupon is left. The
  sample can't settle this: the only bonds in the sample with one coupon left
  are stale Old GoG rows. Implemented in `app/bond_math/`.
- **Don't copy US conventions.** No 32nds, no ACT/360 money-market basis, no
  bond-equivalent yield conversion for bills.

## How the conventions were checked

A throwaway script fitted every combination of settlement lag (T+0/1/2),
frequency (1/2/4), day count (ACT/ACT, ACT/365, ACT/364, ACT/360) and
clean/dirty pricing against all 91 bills and 43 priced bonds in the sample.

**Bills:** simple interest, ACT/364, T+0 reproduces every closing price to
1e-14. The next best (ACT/365, T+0) is off by up to 0.022. Discount-rate
pricing has a worst-case error of 0.79–0.98 under every day count.

**Bonds:** semi-annual clean ACT/ACT, T+0 is the best fit (median price error
0.042). ACT/364 and ACT/365 at T+0 to T+2 come within 0.007 of it, so the
sample can't separate them. Solving for the yield in each reported price:

| Group | Bonds | Implied − reported yield |
|---|---|---|
| New GoG | 2 | 0.0 to +0.1bp |
| 2023 DDEP A/B/GC | 16 | −2.1 to +3.2bp |
| Old GoG | 17 | 8 within ±5bp; the other 9 are 10bp to 13 points off (e.g. 19/11/26: 6.62% reported, 10.23% implied; 08/03/27: 30.15% vs 43.16%) |
| GFSF | 4 priced | +24 to +47bp, and GFSF-1-4YR at 32.76% vs 85.71% |
| USD DDE | 4 | +22 to +188bp |

The New and DDEP residuals are small enough to be rounding or a slightly
different valuation date. The Old GoG outliers are stale prices or stale
yields: these rows rarely trade, and in most of them the opening yield equals
the closing yield and the day range comes from an earlier session.

### Instruments the conventions don't fit

- **GFSF bonds (Financial Stability Fund, issued Nov/Dec 2023).** Priced
  consistently below a plain bullet at the stated coupon. Possibly amortising,
  capitalised interest, or a coupon schedule different from the description.
  Needs the term sheet.
- **USD DDE bonds (`USD-DDE-FCA/FEA-27/28`).** USD, 2.75%/3.25%. Prices are
  0.2–1.6 points below a bullet. Possibly amortising or a different accrual.
  Also note quirk 12: their sell/buy-back "yield" is a price. Needs the term
  sheet.
- **Corporates.** Three descriptions have no coupon, and there is no yield
  column. Needs term sheets.

## Licensing and redistribution

- **GSE market data price list (April 2024)** covers GSE equities and indices.
  In GHS for local and US$ for global licences, per year:
  - End-of-day data licence: GH¢15,000 / US$5,000.
  - Website and mobile end-of-day licence: GH¢15,000 / US$5,000, priced
    **per domain**.
  - Real-time licence: GH¢40,000 / US$14,000.
  - **Non-display licence: US$19,500.** It covers "instrument pricing",
    "portfolio valuation" and risk management. Computing yields, durations and
    curves from GSE data would likely need it.
  - **New Original Works (derived data): US$19,500.**
  - There is no GFIM or fixed income product on the list.
- **BoG auction rates** are public on the BoG website. No API, and no data
  terms found. Using and citing the rates is low risk; confirm before
  redistributing them in a commercial app.
- **Open with the GSE (add to #15):**
  - Does a GSE licence cover GFIM data, or is it licensed separately?
  - Does displaying yields and prices computed from GFIM data to users at other
    firms (Symphony Marketplace) need the derived-data or non-display licence?

## Questions for the GSE (add to #15)

1. Does the API carry GFIM data? End of day only, or intraday trades and
   quotes? At what update rate?
2. Does it deliver yield and price for government securities, and price only
   for corporates, as the report does?
3. Which day count does GFIM use for bond yields and accrued interest (ACT/ACT,
   ACT/365 or ACT/364)? The sample fits all three to within 0.007 in median price.
4. What exactly is the "end of day closing price methodology"? In the sample,
   government closes often fall outside the day's traded range.
5. Will benchmark securities be flagged in the data? The report's note says
   they are "coloured red", but no row is red in either the workbook or the
   PDF, and the PDF truncates the note. So we can't tell which ones are
   benchmarks today.
6. Are there term sheets or an API field for amortisation and coupon schedules
   (GFSF, USD DDE, corporates)?
7. Are coupon and maturity dates adjusted for holidays, and by which rule?
8. Licensing: see [above](#licensing-and-redistribution).

## References

- GFIM Rules, 5 April 2022: Rules 12 (hours), 13 (methodology), 14 (quote
  basis), 16–17 (standard amount, spread), 23 (4 dp prices), 28 (T+2).
  <https://gfim.com.gh/wp-content/uploads/2023/04/GFIM-RULES.pdf>
- CSD, T+2 for all GFIM debt securities from 14 Sep 2015:
  <https://graphic.com.gh/business/business-news/central-securities-depository-announces-change-in-settlement-cycle-for-debt-securities.html>
- BoG T-bill rates: <https://www.bog.gov.gh/treasury-and-the-markets/treasury-bill-rates/>
- GSE data services and price list:
  <https://gse.com.gh/data-services/>,
  <https://gse.com.gh/wp-content/uploads/2025/01/GSE-Market-Price-List-2024.pdf>
- SEC Guidelines on Dealing in GoG Securities 2025 (dealers publish daily
  quotes):
  <https://gfim.com.gh/wp-content/uploads/2025/07/Guidelines-On-Dealing-In-Government-Of-Ghana-Securities-2025-2.pdf>
- Sample report: `docs/samples/gfim-trading-report-2026-09-28.xlsx`.
