# Company profile seed data: sources and conventions

`data/company_profiles.json` holds the company information behind
`GET /instruments/{symbol}/description` (format: `app/company/__init__.py`).
Every value carries its own `source` and `as_of`; this page records the
conventions behind them and what is still missing.

## Coverage

Seeded so far: **MTNGH**, **GCB**. Every other listed equity has no entry
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
