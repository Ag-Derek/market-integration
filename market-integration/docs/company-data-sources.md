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

Seeded so far: **MTNGH**, **GCB**, **ADB**, **ACCESS**, **ASG**, **CAL**, **BOPP**, **ALLGH**, **AGA**, **DIGICUT**, **EGH**, **SCB**, **SOGEGH**, **GOIL**, **TOTAL**, **RBGH**, **SIC**, **KASA**, **MAC**, **SWL** (delisted), **TLW**, **TBL** (delisted), **ZEN**, **IIL**.

Basic profiles only (description, classification, website, headquarters,
listing date; no officers or financials yet): **UNIL**, **FML**, **CLYD**, **CMLT**, **CPC**, **DASPHARMA**, **EGL**,
**ETI**, **FAB**, **GGBL**, **GLD**, **HORDS**.

Second securities of an issuer profiled above: **SCB-PREF** (SCB's
preference shares) and **AADS** (AGA's Ghanaian depositary shares). Their
files describe the security, not the company: the issuer's description,
sector, website and officers are repeated, but they carry no revenue,
earnings or book value. Using the issuer's ordinary-share figures would
make P/E, P/B and market cap wrong for a security priced differently.
The page's link to the issuer (`related_symbols`) leads to the company's
results.

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
- **GLD** is an exchange-traded product, not an operating company. It is
  described from the issuer's GSE releases (PR 257/2012, PR 313/2022,
  PR 241/2025), its statements to 31 March and 30 September 2026, and
  Absa's NewGold minimum disclosure document (December 2023), the only
  source read that gives the ~1/100 oz per unit. The GSE listed-company page
  is almost empty. Left out: `employees` (a special-purpose issuer with no
  staff of its own), `headquarters_city` and `registrar` (no source read
  names them), and `shares_outstanding`. The GSE figures (0.40 million on
  the listed-company page, 3,400,000 units approved, 1,400,000 on the
  Ghana register in 2022) cover only part of a programme that trades
  mainly on the JSE, and no filing read gives the total units in issue, so
  a market cap from any of them would be wrong. Its ISIN is ZAE000060067
  (from PR 241/2025); the instrument master has none.
- **HORDS** trades on the Ghana Alternative Market (GAX), not the main
  board. Its 2025 financial statements (GSE PR 140/2026) are a scanned
  image with no text layer; they were read from the page images. The 2024
  annual report (PR 473/2025) is mostly images too, so only its chairman's
  address was used. Left out: `employees` (no year-end headcount in any
  filing; the 2024 chairman's address gives only the 19 staff who restarted
  production in December 2024), `website` and `registrar` (none given in
  the filings or on the GSE page).
- **UNIL** is described from its 2023 annual report, the latest full
  report on gse.com.gh.
- **SCB**'s financials come from its summary financial statements for
  2022, 2024 and 2025 (GSE PR 114/2023, PR 094/2025, PR 075/2026). On
  gse.com.gh each PR is a one-page cover note; the statements are a
  separate attachment with no text layer, read from rendered page images.
  The 2023 annual report (PR 095/2024) is an OCR'd scan, used for the
  share count, registrar and corporate information. The FY2024 summary
  restated 2023 (operating income GHS 1,719.9m to 1,643.6m, profit
  869.3m to 819.7m, EPS 6.45 to 6.08) and the 1 January 2023 equity
  (GHS 1,329.2m to 1,288.9m, used as the FY2022 book value); the restated
  figures are used. FY2021 is bank-only, because the wealth management
  subsidiary was set up in 2022. Shares outstanding (134,758,498) count
  ordinary shares only. The GSE listed-company page shows 134,763,467 and
  still names Mansa Nettey as Managing Director, but the 2026 filings name
  Xorse Godzi. Dividends are ordinary only; the preference shares get
  separate half-yearly payments.
- **SOGEGH** reports bank-only figures: its one subsidiary, SSB
  Investments, has been dormant since 2003. Financials come from the
  2023–2025 annual reports (GSE PR 074/2024, PR 070/2025, PR 061/2026)
  and the 2022 audited statements (PR 138/2023), which give 2021 as the
  comparative. Some of these filings are listed on gse.com.gh as "Societe
  General", without the final "e", and a search under the full name misses
  them. The 2023 report's "restated" comparatives only reclassify property
  and intangible assets; total equity and profit are unchanged. `employees`
  is the 2025 average (536), the only staff figure the report gives. The
  shareholding (Societe Generale group 60.22%) is pending sale: on
  1 October 2026 the group agreed to sell 55.22% to Attijariwafa Bank and
  5% to SSNIT, subject to regulatory approval (PR 395/2026). The 2026
  dividend payment (GHS 173.7m) is slightly more than GHS 0.24 × 709.1m
  shares (GHS 170.2m); the dividend per share follows the AGM resolution.
- **GOIL**'s financials come from its consolidated statements for
  2021–2025 (GSE PR 116/2022, PR 137/2023, PR 096/2024, PR 135/2025,
  PR 181/2026); none restates a prior year. Revenue is **net revenue**,
  meaning gross revenue less customs duties and levies (GHS 1.3bn in
  2025), the line the cost of sales is set against. Net revenue nearly
  tripled from 2021 to 2022 with fuel prices and the cedi's fall. Net
  income is the group profit after tax; GOIL has no non-controlling
  interest, because its 60% of African Bitumen Terminal is accounted for
  as a joint venture. The FY2025 dividend (GHS 0.06) was proposed for
  the 57th AGM on 11 June 2026; no resolutions notice is on gse.com.gh,
  but the 30 September 2026 statements show GHS 23.2m paid in 2026. The
  registrar changed from MBG Registrars (2024 notice) to NTHC Registrars
  (2025 and 2026 notices).
- **TOTAL**'s financials come from its 2022–2025 annual reports (GSE
  PR 193/2023, PR 160/2024, PR 104/2025 with the chairman's statement in
  PR 235/2025, and PR 113/2026). Revenue excludes the taxes and levies
  collected for the government. The 2025 report switched to this basis
  (2024: GHS 6,137m instead of 7,021m), and its five-year highlights give
  2021–2023 on the same basis. TOTAL owns 55% of Ghanstock, whose minority
  interest is in deficit, so net income and book value are the owners'
  share: profit attributable to owners, and total equity less the
  non-controlling interest (e.g. 2025: 552.6m + 17.3m = 569.9m). EPS is
  as reported, but the basis is inconsistent: through 2024 the reports
  divide owners' profit by 111.87m shares, while the 2025 report uses
  total profit including the minority (2025: 2.8522; 2024 restated
  2.6070 to 2.6094). 2024 keeps the owners'-basis 2.6070. Dividends are
  interim plus final, each with its own GSE closure-of-register notice.
  Every recorded year therefore has two payment dates, and the total-return
  calculation splits the year's dividend equally between them, so it is
  flagged as estimated. PR 398/2023 (2023 interim) is printed "2 November
  2022", a typo for 2023.
- **RBGH**'s financials come from the summary statements for 2021, 2024
  and 2025 (GSE PR 054/2022, PR 066/2025, PR 056/2026) and the 2023 annual
  report (PR 272/2024), which gives 2022 as the comparative; the 2022
  audited statements (PR 104/2023) are a scanned image. The GSE page and
  file for the FY2025 summary (PR 056/2026) are mislabelled "2021" and
  "2022"; the same file is on republicghana.com, along with the 2025
  annual report, which was used for EPS, headcount and shareholders. Net
  income and book value are the owners' share, because subsidiaries have
  minority holders (2025: GHS 11.0m of profit, 30.7m of equity). The 2021
  dividend (GHS 0.025, PR 072/2022) was declined by the Bank of Ghana and
  withdrawn (PR 267/2022), and it was never paid, so FY2021 is recorded as
  0.0. The registrar is Universal Merchant Bank per the 2025 annual report;
  the GSE notices' distribution lists still say MBG Registrars, and the
  GSE page still gives the old North Ridge head office.
- **SIC**'s audited statements for 2022, 2024 and 2025 (GSE PR 289/2023,
  PR 353/2025, PR 247/2026) are 95–100-page scans with no text layer.
  They were read from rendered page images (financial highlights,
  statements of financial position and profit or loss, auditor's report,
  corporate information). The 2023 annual report (PR 399/2024) has text.
  Revenue is IFRS 17 **insurance revenue**, available from 2022, which
  the 2023 report restated. 2021 is under IFRS 4 (gross premium
  GHS 276.4m), with no comparable figure, so 2021 `revenue` is left
  blank. Its profit (GHS 60.3m) and EPS are also on the IFRS 4 basis, and
  its book value is the IFRS 17 restated opening equity at 1 January 2022.
  Net income and book value are the owners' share; the non-controlling
  interest is in SIC Financial Services. SIC's EPS divides total group
  profit, including the non-controlling interest, by 195,645,000 shares.
  The auditor (Baker Tilly) qualified the 2024 and 2025 opinions. In
  2025 the basis was legal liabilities at SIC Financial Services that may
  be understated: GHS 21m provided against a judgment confirmed at about
  GHS 95.7m, and no provision for a Bank of Africa judgment of about
  GHS 68m including interest. The reported figures are used as audited.
- **KASA** listed on 15 June 2026 after an IPO of 583,333,333 new shares
  at GHS 1.20 (GSE PR 258/2026). Every reported year predates the
  listing. Financials are company-only (no subsidiaries are consolidated)
  and come from the 2023–2025 annual reports (GSE PR 110/2024,
  PR 093/2025, PR 097/2026); 2021 and 2022 come from the 2025 report's
  five-year summary and the 2023 report's comparatives.
  EPS uses the 3.55bn pre-IPO shares, while `shares_outstanding` is the
  post-IPO 4,133,333,333 (3.55bn plus the IPO shares; no filing states
  the total), so a P/E built from them mixes bases. Book value at end-2025
  (GHS 700.4m) excludes the GHS 700m raised in 2026. The 2025 report's
  capital-management note gives total equity as GHS 700,760,254, against
  700,435,416 in the statement of changes in equity; the latter is used.
  `employees` is permanent staff (826); casual and contract workers bring
  the 2025 total to 2,391. No dividends were paid in 2021–2025, and the
  note programme bars them for 2024–2026 unless waived.
- **MAC** is an investment company, so its financials come from its
  annual reports for 2022, 2024 and 2025 (GSE PR 343/2023, PR 340/2025,
  PR 386/2026). 2023 and 2021 come from the comparatives in the next year's
  report. All figures are company-only. The text layers are badly laid
  out, with columns out of order, so each figure was checked against the
  statement of changes in equity or the chairman's review. Revenue is
  "total revenue": dividend and rental income only. Fair value changes on
  investments go through other comprehensive income and are not counted.
  The net losses (GHS 29.0m in 2025) come mainly from finance costs, which
  include exchange losses. No filing gives EPS or a headcount, so both are
  left blank. EPS works out at about GHS -2.92 for 2025 from the loss and
  9,948,574 shares, but it is not recorded because the company does not
  report it. The 2023 book value (GHS 97,376,482) follows the statement of
  changes in equity; the balance sheet's 2023 column shows 97,386,481. The
  2025 report's corporate information says Christine Dowuona-Hammond
  resigned as Chairperson on 1 September 2025, and its governance report
  names Eugene Addison as Chairperson. Its top-20 shareholder table does
  not line up (Oak Partners Limited is listed first, but the share counts
  and percentages do not match 9.95m shares), so no ownership is given.
  The share count is the report's 9,948,574; GSE PR 209/2016 gave
  9,948,576. The last dividends were for 2014 (GHS 0.05) and 2015
  (GHS 0.06), before the years recorded. The website answers over http
  only.
- **SWL** (Sam-Woode Ltd) is **no longer listed**. The GSE suspended it
  on 29 September 2020 after it stopped operating (PR 396/2020) and
  compulsorily delisted it on 14 April 2023 (PR 102/2023), because it
  never completed the voluntary delisting its shareholders approved in
  November 2019. The instrument master had it as an active main-board
  stock named "Sam Wood Ltd". It is now `status: "delisted"` and named
  Sam-Woode Ltd. Its mock price is still a placeholder; it has had no
  quote since 2020. The GSE listed-companies page still lists it. Its
  financials stop at 2019. The audited 2019 and 2020 statements
  (PR 421/2021) are dead links on gse.com.gh, so 2019 comes from the
  **unaudited** full-year statements (PR 065/2020), and each 2019 source
  says so. 2017 and 2018 come from the audited 2018 annual report
  (PR 253/2019), a scan with no text layer, read from page images. That
  report prints EPS "in Ghana Pesewas", but the figures are cedis
  (2018: GHS 1,471,127 loss / 21,828,035 shares = 0.0674). The 2017 EPS
  is printed rounded (0.03). Officers are those named in the 2018 report;
  no later list was filed. No website: samwoode.com returns a server
  error, and swllearn.com is the planned subsidiary's app, not the
  company's site.
- **TLW** (Tullow Oil plc) reports in US dollars, which its records keep
  (like ASG), although its GSE shares trade in cedis. Financials come
  from the 2025 and 2024 full-year results (GSE PR 167/2026,
  PR 087/2025) and the 2022 Annual Report and Accounts (PR 079/2023).
  The 2023 annual report and full-year results are not on gse.com.gh
  (PR 056/2024 is a cover note only), so 2023 comes from the 2024 results'
  comparatives. EPS is converted from US cents (0.4 cents = 0.004).
  Following "restated figures win", 2024 revenue is the 2025 report's
  US$1,287.2m, which excludes Gabon (sold July 2025, now a discontinued
  operation), not the US$1,534.9m first reported. 2021–2023 revenue still
  includes Gabon, so the series has a break at 2024. Net income is the
  profit for the year, including discontinued operations. That matches
  the reported EPS: in 2025, a US$129.2m continuing loss plus US$135.7m
  from Gabon gives US$6.5m. There is no non-controlling interest. Book
  value is negative in every year (net liabilities), so P/B is
  meaningless. No dividends appear in the equity statements for
  2021–2025. The share count is the 31 May 2026 total voting rights
  (PR 249/2026). The registrar is the CSD for the Ghana register
  (Computershare keeps the UK register). `employees` is left blank: the
  2025 annual report on gse.com.gh is a cover note, and the 2022 figure
  predates the Gabon and Kenya sales.
- **TBL** (Trust Bank PLC, The Gambia) is **no longer listed** on the
  GSE. Shareholders approved delisting at the July 2024 AGM (PR 293/2024).
  Solo Dabo Company Limited's tender offer at GHS 1.20 drew 47.89% of the
  7,328,773 Ghana-register shares, and the shares were delisted on
  25 September 2026 (PR 351/2026, PR 391/2026). The instrument master
  now has `status: "delisted"` and the current name, Trust Bank PLC (The
  Gambia). The GSE listed-companies page still lists it. Financials are
  group figures in Gambian dalasi (GMD), from the 2021–2025 annual reports
  (PR 093/2022, PR 150/2023, PR 162/2024, PR 180/2025, PR 231/2026).
  Revenue is total operating income, as for the other banks. Restated
  figures are used: 2023 operating income (D1,069.0m, was 1,023.3m) and
  2022 profit (D248.6m, was 248.2m). **Share basis break:** in 2025 the
  bank doubled its shares from 200m to 400m by a D200m transfer from
  statutory reserves, and the 2025 report restates 2024 EPS from D1.70 to
  D0.85. EPS for 2024–2025 is on 400m shares, and 2021–2023 is as
  reported on 200m shares (halve those figures to compare). Dividends per
  share are as declared. The D180m of dividends paid in 2025 matches
  D0.55 + D0.35 on 200m shares, so the 2025 interim was evidently paid
  on the old count. The 2025 report's note 13 gives the 2024 total as
  D0.90, but the GSE notices (D0.30 + D0.55) and that report's own
  highlights say D0.85; D0.85 is used. The 2021 EPS is the 2021 report's
  group D0.72; the 2022 report's comparative (75 bututs) is the
  bank-only figure. `shares_outstanding` is the 400m.
- **ZEN** (ZEN Petroleum Holdings PLC) listed on 22 April 2026 after an
  IPO of 128m new shares at GHS 5.00, from its prospectus dated 17 March
  2026. The holding company was only incorporated on 9 December 2025, so
  FY2023–FY2025 (years to 31 March) come from the reporting accountant's
  **combined special purpose** statements of the five subsidiaries
  (prospectus Appendix D). These are not consolidated group accounts.
  Net income is profit after tax before the foreign currency translation
  difference, which the prospectus labels confusingly ("profit for the
  year" is used for both lines). No EPS or dividend per share is recorded
  for those years: there was no share basis before the holding company
  existed, and the combined dividends (GHS 51.3m, 175.6m, 135.9m) were
  paid by the subsidiaries. **FY2026** has only `book_value`. No annual
  report for the year to 31 March 2026 has been published: the company's
  site says its first report is "not yet due", and nothing is on
  gse.com.gh. The H1 2026/27 statements give the audited 1 April 2026
  equity of GHS 198.9m. That is after a GHS 600m dividend recognised in
  FY2026 and paid in April–June 2026. The Q1 statements had shown
  GHS 320.6m before the audit adjustments. The H1 report refers to a note 6
  on those adjustments, but the PDF ends at note 3. The big fall from
  GHS 943.6m at March 2025 is mostly that dividend. Its per-share amount
  is not stated, and it was paid while the 128m IPO shares were being
  issued, so it is not recorded. `shares_outstanding` is 512m (prospectus)
  plus the 128m IPO shares. The GSE listed-companies page shows
  128,000,000, only the IPO shares, and its company link returns 404. The
  H1 EPS (GHS 0.27 on GHS 172.6m) matches 640m. The website gives "over
  1,300 staff", and both 58 and 63 stations, so `employees` is left
  blank and the description says "more than 60" stations. Mr Lithur
  joined the board as an independent director on 9 October 2026
  (PR 454/2026); the officers listed are the chair, MD and finance
  director.
- **IIL** (Intravenous Infusions PLC) trades on the GAX. Its annual
  reports for 2022–2025 (GSE PR 103/2023, PR 117/2024, PR 136/2025,
  PR 147/2026) and the H1 2026 statements (PR 329/2026) are scans with no
  text layer. On gse.com.gh each is a one-page cover note plus a separate
  attachment. They were read from rendered page images. Figures are
  company-only: the FlexPharm subsidiary (GHS 9,720 investment) is not
  consolidated. EPS is printed as "pesewa", but the figures are cedis
  (2025: GHS 4,631,793 / 274,408,346 = 0.0169). The 2022 report gives no
  EPS, so 2022 comes from the 2023 report's comparative, and 2021 is
  blank. The 2022 book value follows the 2024 report's 1 January 2023
  balance (GHS 14,778,726); the 2022 report shows 14,778,723. The FY2023
  dividend is GHS 0.0028 per the payment notice (PR 266/2025). The 2024
  report's directors' report says GHS 0.004, but its total (GHS 768,343)
  is 0.0028 a share. It was approved at the 2024 AGM but only paid on
  18 July 2025. **Share count:** `shares_outstanding` is the 274,408,346
  at end-2025. The GSE page's 258.82m is stale. In February 2026 shareholders
  approved a GHS 50m rights issue and private placement, and converting
  GHS 5.04m of loans from CCM Healthcare Investments and AI Acquisitions
  into shares (PR 088/2026). The conversion took effect on 9 August 2026
  (PR 455/2026), and stated capital is GHS 16.4m at 30 June 2026. No filing gives the
  number of new shares or the conversion price, so the count, and any
  market cap from it, understates the current total. The H1 2026 EPS
  (GHS 0.0005) is too rounded to back out a share count. The 2025
  auditor (Intellisys) flagged material uncertainty over going concern.
  Ernst & Young was appointed auditor at the October 2026 AGM. The
  registrar is GCB Bank's Share Registry, per the 2026 AGM notice; the
  GSE distribution lists name NTHC Registrars. That notice gives the
  registered office as No. 9 Carrot Avenue, East Legon, which is the
  company secretary's address. The annual report and the 2026 letterhead
  both give Koforidua, which is used.
- **SCB-PREF** dividends are recorded by the calendar year of the
  half-year periods they cover ("March 2024", "September 2024"), as the
  bank's notices name them. That differs from how the AGM press reports
  group them (e.g. GHS 2.77m of "preference dividends" for FY2024). The
  rate floats (16.2% in March 2022, 33.9% and 31.2% in 2024). Only years
  with a complete primary source are recorded: 2022 (GHS 0.09, as printed
  in the 2023 annual report's note 17), 2023 (none, after the debt
  exchange) and 2024 (GHS 0.0879 + 0.0810). 2025 and 2026 are left out:
  the September dividends (GHS 0.0512, PR 347/2025; GHS 0.0249,
  PR 381/2026) are on gse.com.gh, but the March 2025 and March 2026
  notices are not. The dividend yield on the page therefore uses 2024,
  which overstates the current yield as rates have fallen. The listing
  date is left blank, because the GSE page gives the issuer's 1990 date.
- **AADS**: 100 GhDSs represent one AngloGold Ashanti plc ordinary share,
  with NTHC Limited as depositary. The GSE trades them under the code AAD.
  The listing date is the plc's GhDS listing (26 September 2023), which
  replaced the predecessor's GhDSs listed since 27 April 2004. The GSE
  listed-company page is years out of date (chairman R. P. Edey, 0.98
  million "issued shares"), and no filing read gives the number of GhDSs
  in issue, so `shares_outstanding` (and market cap) is left blank. No
  dividends are recorded: AngloGold declares them in US dollars per
  ordinary share, and no filing gives the cedi amount paid per GhDS.

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
- **Revenue for insurers** (SIC) is IFRS 17 insurance revenue. Years
  reported only under IFRS 4 (gross premium) have no revenue figure,
  because the two are not comparable.
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
| SCB | `employees` | Neither the 2023 annual report nor the summary financial statements give a headcount. |
| SCB | FY2022 `dividend_payment_dates` | No dividend for FY2022. |
| SOGEGH | FY2024 `dividend_payment_dates` | The 45th AGM resolutions (PR 246/2025) approve the GHS 0.34 dividend but give no payment date, and no 2025 closure-of-register notice is on gse.com.gh. It was paid in 2025 (2025 Annual Report, note 31). |
| SOGEGH | FY2022–FY2023 `dividend_payment_dates` | No dividend for those years. |
| GOIL | `employees` | The annual reports give no headcount. |
| GOIL | `officers` (finance) | Neither the annual report nor the interim statements name a CFO or finance director. |
| TOTAL | FY2021 `dividend_payment_dates` | The 2021 closure-of-register notices on gse.com.gh (PR 225, 226 and 417 of 2021) are scanned images with no text, and no 2022 notice for the 2021 final dividend was found. |
| RBGH | FY2021–FY2024 `dividend_payment_dates` | No dividend paid for those years. |
| SIC | FY2021 `revenue`; `employees` | 2021 was reported under IFRS 4 (gross premium), with no IFRS 17 insurance-revenue figure. No headcount was found in the filings read. |
| SIC | FY2022 `dividend_payment_dates` | No dividend for FY2022. |
| KASA | FY2021–FY2025 `dividend_payment_dates` | No dividend paid in those years. |
| MAC | `employees`; FY2021–FY2025 `eps`, `dividend_payment_dates` | The filings give no headcount and no EPS. No dividend was paid in those years. |
| SWL | FY2020 onward; `employees`, `website`; `officers` (finance) | Delisted 14 April 2023. The audited 2019 and 2020 statements are dead links on gse.com.gh, and nothing later was filed. The 2018 report gives no headcount and names no finance officer. |
| TLW | `employees`; FY2021–FY2025 `dividend_payment_dates` | The 2025 annual report on gse.com.gh is only a cover note, and the 2022 headcount predates the asset sales. No dividend was paid in those years. |
| TBL | FY2021, FY2022, FY2025 `dividend_payment_dates`; `officers` (finance) | The 2021 notices on gse.com.gh are dead links, and the 2022 interim notice was not found. No notice of the 2025 final dividend (proposed for the June 2026 AGM) was filed before delisting. The annual report names no CFO. |
| ZEN | FY2026 `revenue`, `net_income`, `eps`, dividends; FY2023–FY2025 `eps`, dividends; `employees` | The FY2026 annual report is not yet published. Before the December 2025 holding company there was no share basis, and the subsidiaries paid the dividends. The website's headcount ("over 1,300") is approximate. |
| IIL | `employees`; FY2021 `eps`; FY2021–FY2022, FY2024–FY2025 `dividend_payment_dates` | The filings give no headcount. The 2022 report gives no EPS, and 2021 has no later comparative. No dividend was declared for those years. |
| SCB-PREF | FY2025–FY2026 dividends; FY2022 `dividend_payment_dates`; `listing_date` | The March 2025, March 2026 and September 2022 preference dividend notices are not on gse.com.gh. The GSE page gives only the issuer's listing date. |
| AADS | `shares_outstanding`; dividends | No current count of GhDSs in issue (the GSE page's 0.98m predates 2023). Dividends are declared in USD per ordinary share, with no published cedi amount per GhDS. |
| TOTAL | `officers` (finance) | The 2025 annual report names no CFO; Mercy Samson, the investor relations director, is listed instead. |
