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

Seeded so far: **MTNGH**, **GCB**, **ADB**, **ACCESS**, **ASG**, **CAL**, **BOPP**, **ALLGH**, **AGA**, **DIGICUT**, **EGH**. Every other listed equity has no entry
yet and is served as all-null.

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
