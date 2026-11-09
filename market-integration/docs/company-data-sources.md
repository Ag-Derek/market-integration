# Company profile seed data: sources and conventions

`data/companies/` holds the company information behind
`GET /instruments/{symbol}/description`, one file per company named after
its symbol (`data/companies/EGH.json`; format: `app/company/__init__.py`).
Every value carries its own `source` and `as_of`; this page records the
conventions behind them and what is still missing.

To add a company, add its file. The tests load every file strictly and
fail on a bad one. At startup a bad file is logged and skipped, so only
that company's description goes blank.

## Coverage

Seeded so far: **MTNGH**, **GCB**, **ADB**, **ACCESS**, **ASG**, **CAL**, **BOPP**, **ALLGH**, **AGA**, **DIGICUT**, **EGH**.

Basic profiles only (description, classification, website, headquarters,
listing date; no officers or financials yet): **SCB**, **SOGEGH**, **GOIL**,
**TOTAL**, **UNIL**, **FML**, **CLYD**, **CMLT**, **CPC**, **DASPHARMA**, **EGL**,
**ETI**, **FAB**, **GGBL**.

Every other listed equity has no file yet. Its API fields are all null,
and the description page shows a one-line summary from the instrument
master (name, board, sector) instead.

Notes on the basic profiles:

- **The GSE listed-company pages are partly out of date** (old company
  names, dead websites, stale share counts). They are used for listing
  dates and registered offices; websites were checked live.
- **FML** is described from its 2022 annual report: the 2023 and 2024
  reports on gse.com.gh are scanned images with no readable text. Its
  ownership (Fan Milk International A/S, 62.11%) is as at end-2022.
- **CMLT** is described from its 2023 annual report (GSE PR 153/2024): the
  2024 report on gse.com.gh is a scanned image with no readable text. Its
  registrar (MBG Registrars) comes from the GSE release of that 2024 report.
- **CPC** is described from its Q3 2026 interim statements (GSE PR 354/2026),
  which carry current corporate information, and its 2018/2019 annual report
  (GSE PR 501/2020) for the nature of business, incorporation date and
  shareholders: no later full annual report is on gse.com.gh. Its ownership
  (COCOBOD 57.73%, Government of Ghana 26.13%, SSNIT 10.14%) is as at
  30 September 2019, before the 2022 conversion of US$87m of COCOBOD debt
  into Class B shares, which are still unregistered. No website: its
  goldentreeghana.com domain redirects to cpc.com.gh, which did not respond.
- **DASPHARMA** is described from its 2024 annual report (GSE PR 364/2025);
  the audited 2025 accounts were delayed with SEC approval and are not on
  gse.com.gh. The GSE listed-companies page leaves its listing date blank,
  so it comes from the merger announcement (PR 006/2020): the merged
  company's shares began trading on 15 January 2020. No website: dasplc.net,
  given in its 2025 EGM notice, does not resolve, and dasplc.com belongs to
  an unrelated business.
- **EGL** is described from its 2023 annual report (GSE PR 214/2024), the
  latest full report on gse.com.gh, with its registered office from the
  2024 summary financial statements (GSE PR 144/2025). Its ownership
  (Grace Strategic Ventures 44.12%) is as at end-2023. No listing date:
  the GSE listed-companies page leaves it blank, the annual report does not
  give one, and secondary sources disagree (1991, for the predecessor
  Enterprise Insurance, versus November 2010).
- **ETI** is described from the Ecobank Group Annual Report 2025 and the
  9M 2026 interim statements, both from ecobank.com; ETI's own filings are
  not on gse.com.gh. Its ownership (Bosquet Investments 21.22%, Qatar
  National Bank 20.10%) is as at end-2025, after Nedbank sold its stake to
  Bosquet in December 2025. ETI reports in US dollars, but its GSE shares
  trade in cedis. The share count is the annual report's 24,730,354,443
  issued; the GSE page shows an older 24,067.75 million. No registrar:
  neither report names one for the Ghana register. The country count
  differs by source (32 with banking operations in the directors' report,
  34 in the group overview, "presence in 38" in the interim notes); the
  description uses 32.
- **FAB** is described from its 2025 annual report and audited statements
  and its December 2025 IPO prospectus, all from firstatlanticbank.com.gh.
  The GSE page's "date incorporated" (7 November 2025) is the date it
  re-registered as a PLC; it was first incorporated on 4 May 1994. Its
  ownership (Kedari Nominees 46.6%, AFIG Fund II 21.0%) is as at end-2025,
  after the IPO. The instrument master has no ISIN for it.
- **GGBL** is described from its 2025 annual report (GSE PR 375/2025, year
  to 30 June 2025), the latest full report on gse.com.gh. Its ownership
  there (Diageo 80.4%) predates the sale of that stake to Castel's Boissons
  Nouvelles Holding on 3 July 2025; no later shareholder list has been
  filed. The GSE listed-company page is wrong on two counts: 100 million
  issued shares (the register shows 307,594,827) and www.diageo.com as the
  website. The company's own site, guinnessghanaplc.com, refuses automated
  requests and could not be checked live. GGBL has changed its year end
  to 31 December (PR 328/2026): its current financial period runs 18 months,
  from 1 July 2025 to 31 December 2026, so any financials added later need
  that period treated as a single fiscal year.
- **SCB** and **UNIL** are described from their 2023 annual reports, the
  latest full reports on gse.com.gh.

## Conventions

- **Primary sources first.** Figures come from the company's own audited
  results, annual reports and summary financial statements, mostly as filed
  on gse.com.gh. A news report is used only where no company filing could be
  reached, and the `source` says so.
- **Group figures** are used where a company reports both group and
  company-only numbers.
- **Amounts are in full currency units** (GHS), converted from the GHS '000
  used in the filings.
- **Revenue for banks** is total operating income (net interest, fees,
  trading and other income). Gross interest income would overstate it.
- **Restated figures win.** When a later report restates a prior year, the
  restated number is used and the `source` says "restated".
- **Dividend per share** is the total declared for the fiscal year
  (interim + final). `0.0` means no dividend was declared for that year, and
  its source is the statement that shows none.
- **Revenue for pre-production companies** (e.g. ALLGH) is `0.0`, with the
  filing's statement that the group has no revenue as its source. Interest
  and fair value gains are not counted as revenue.
- **Foreign-reporting companies** keep their own currency and year end
  (ASG in USD, ALLGH in AUD to 30 June); `fiscal_year` is the calendar year
  the financial year ends in.
- **as_of** is the fiscal year end for financial figures, the board
  approval or declaration date for dividends, and the report or page date
  for profile fields and officers. Values read from a live web page (e.g.
  the GSE listed-company page) use the date it was read.
- **Descriptions** are our own words, written from the filings. No text is
  copied from annual reports, Bloomberg or other sites.

## Missing values

Left out (served as null) because they couldn't be found or confirmed:

| Symbol | Field | Reason |
| --- | --- | --- |
| MTNGH | FY2023 `dividend_payment_dates` | MTN's own documents disagree on the interim payment date (8 Sep 2023 in the FY2023 results, 15 Sep 2023 in the 2023 Annual Report). |
| MTNGH | FY2022 `dividend_payment_dates` | The 2022 Annual Report gives the final date only as planned "if approved", and part of that final dividend was paid as scrip. |
| GCB | `employees` | Not in the summary financial statements. The full annual report is on gcbbank.com.gh, which blocks automated access. |
| GCB | FY2022–FY2024 `dividend_payment_dates` | No dividend for those years. |
| CAL | FY2021 `dividend_payment_dates` | The 2025 Annual Report gives the GHS 0.11 dividend per share but not when it was paid. No dividend for FY2022–FY2025. |
| BOPP | FY2021–FY2024 `dividend_payment_dates` | The annual reports give only the final dividend's payment date, not the interim's. |
| BOPP | `officers` (finance) | The annual report names no CFO or finance manager. |
| ALLGH | `employees` | The annual reports give no headcount. |
| ALLGH | `dividend_payment_dates` | No dividend for FY2022–FY2026. |
| AGA | `employees` | The 2025 reports give only workforce averages that include contractors (38,243 at managed operations). |
| AGA | FY2021–FY2023 `dividend_per_share`, `dividend_payment_dates` | Before the 2023 redomicile, interims were declared in South African cents (e.g. 70 SA cents for H1 2023), so there is no exact US-dollar total for those years. |
| DIGICUT | `employees`; `officers` (Managing Director) | The audited statements give no headcount, and refer to a Managing Director without naming one. |
| DIGICUT | FY2021–FY2022 `eps` | Only in the five-year summary, which gives no EPS. |
| DIGICUT | FY2021–FY2023 `dividend_per_share` | No directors' report for those years was read; FY2024–FY2025 reports state no dividend. |
| EGH | FY2025 `revenue`, `net_income`, `book_value` | The FY2025 audited summary (released 31 March 2026) could not be downloaded from gse.com.gh or ecobank.com. News reports give only rounded figures (profit GHS 1.82bn, equity GHS 7.2bn) and disagree on revenue (GHS 4.91bn vs 5.21bn). |
| EGH | `shares_outstanding` | The GSE listed-company page shows 293.23 million, which is out of date: the 2024 EPS (527 GHp on GHS 1,699.9m) and the FY2021 dividend (GHS 199.98m at GHS 0.62) both imply about 322.55 million. No filing that was read gives the exact count. |
| EGH | `listing_date`; `employees` | The GSE page gives only "July, 2006". The summary financial statements give no headcount. |
