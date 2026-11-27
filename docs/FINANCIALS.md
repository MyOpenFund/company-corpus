# Financials — curated XBRL concepts, derived metrics & TTM ratios

`company_corpus/financials.py` distils the SEC `companyfacts` XBRL feed into one
**summary per actual reporting period** (annual / quarterly / semi-annual), each
carrying up to 71 curated line items (the concept dictionary), a block of **derived metrics** (single-period),
and a block of **trailing-twelve-month (TTM)** ratios aligned with Bloomberg's
methodology. The full raw JSON is kept separately for exhaustivity.

This document is the reference for what is computed and how. The definitions are
verified against the FASB/SEC US-GAAP XBRL taxonomy and, for the TTM ratios,
against Bloomberg's published quarterly figures.

## Period model

A period is keyed by its **own period-end + frequency** (derived from each fact's
`start`/`end`), so prior-year comparatives carried inside a filing land in their
own period rather than the report's fiscal year. Duration facts (income, cash flow)
define the periods; instant facts (balance sheet) attach by matching end date.
Restatements resolve to the latest `filed`; `publication_date` is the earliest
`filed` for the period, on the SEC path among filings made once the period had ended: a filing
can already hold a fact about a later period, such as a declared dividend. A period only seen
before it ended has no SEC summary. Each value also records its source XBRL **tag** as
provenance.

**Per-period tag resolution.** On the SEC path the value of a concept for a period follows the
selection described in "How a concept gets its value" below: the candidates are the latest known
value of each tag, ranked by position on the statements, then latest filing, then dictionary order.
The tag order alone no longer decides. On the EU and register paths a concept's value still comes
from the **highest-priority fallback tag that actually has a value for that period**, then the
latest-filed point within it. Filers switch tags across taxonomy vintages
(e.g. Microsoft's cost of revenue moved `CostOfRevenue` → `CostOfGoodsAndServicesSold`;
Alphabet's revenue `RevenueFromContractWithCustomerExcludingAssessedTax` → `Revenues`);
a first-tag-wins lookup would return the stale tag and silently drop the recent
period, so every path resolves per period.

Monetary facts are filtered to the issuer's dominant reporting currency, so a
convenience translation (e.g. a USD value alongside a primary EUR one) never gets
summed or divided as if it were the functional currency.

## Curated concepts

The curated line items are data, in two levels (dictionary version 2.1.0):
`company_corpus/concepts/concepts.json` holds the meaning of each concept (label, statement,
instant, `kind` = `money`, `money/shares` or `shares`, `filed_before_period_end`, and the global
tolerances), and one table per
taxonomy, `concepts/tags/<table>.json`, holds that taxonomy's tags (its `taxonomy`, `namespace`, its
`position_version_prefix`, and per concept a `status`, `on_several`, preference-ordered `tags`,
`statement_only_tags` and `computed`). The loaded tables are `us_gaap` and `ifrs_full`
(`DEFAULT_TABLES`). A full tag `ns:Tag` belongs to one table; the same bare name in two namespaces is
two tags; a table may borrow `dei:` cover-page tags. Each concept has one meaning, a
preference-ordered list of tags per taxonomy, and the statement it belongs on. In `tags/us_gaap.json`
48 concepts are `reviewed`; the other 23 keep their earlier chains and are marked `not_reviewed`. Beyond
the core lines the set includes `long_term_investments`, `preferred_stock`,
`noncontrolling_interest`, `equity_total`, `net_income_nci`, `acquisitions_net`,
`debt_proceeds`/`debt_repayments`, `restricted_cash`, `retained_earnings`, `treasury_stock`, `aoci`
and `pension_obligations`.

Figures of different meanings are different concepts: `payables` and `payables_and_accrued`,
`receivables` and `receivables_total`, `net_income` and `net_income_consolidated`, `cash` and
`cash_and_st_investments`, `long_term_debt`, `long_term_debt_lines`,
`long_term_debt_incl_current` and `debt_total`. `equity` is **parent-only**; the
NCI-inclusive figure lives in `equity_total`, so ROE and book value never divide parent income by
parent+NCI equity.

Of the 48 concepts reviewed for US GAAP, 41 are reviewed in the `ifrs_full` table (as reviewed on
NatWest, AstraZeneca and JBS) and 7 are absent from it or `not_reviewed` there (`short_term_debt` is
`not_reviewed`); the table has 10 `not_reviewed` entries in total. Some concepts are deliberately empty or absent for IFRS
filers (the total debt family, `shares_outstanding`, `revenue` and `interest_net` off the statement):
see `tags/ifrs_full.json` for the exact list. Values are stored as filed, in the filing's reporting
currency (GBP, CAD, ...), so the unit is on each value, not on the concept: `concept_catalog()`
entries carry `kind`, not a unit.

Which taxonomy answers (`selection.py`): a position places a fact only through its own taxonomy's
`position_version_prefix` (an extension element never places a dictionary tag). For one concept and
period, one taxonomy answers: that of the latest filing with a candidate (on the same filing, the one
on the statement, then table order), so a filer that moved from IFRS to US GAAP gets one row. A sum
concept's candidates are its statement lines, and a computed total reads its own taxonomy. Note the
asymmetry: within one taxonomy an older statement value beats a newer note; across taxonomies the
newer filing's taxonomy answers. A restatement across taxonomies does not raise `conflict` (the
replaced value is tracked per tag), and a note value from another taxonomy can answer when the
answering taxonomy's tag is statement-only and off the statement.

Tag choices that still matter to a reader:

- `net_income` is the parent's share (`NetIncomeLoss`, after NCI); `ProfitLoss` is the consolidated
  total. A filer whose income statement only has
  `NetIncomeLossAvailableToCommonStockholdersBasic` gets that line, which is after preferred dividends.
  A filer showing both lines with different values is flagged `conflict`, which here only reflects the
  dividends. `net_income_nci` is the NCI part, for reconciliation only.
- `pretax_income` leaves out the domestic-only subtotal: for a multinational it would make the tax
  rate use domestic pretax income. Some filers tag only the domestic and foreign parts
  (`pretax_domestic`, `pretax_foreign`); their sum stands in for pretax income.
- `long_term_investments` are noncurrent marketable securities, which net debt must offset for
  cash-rich issuers. Financial issuers tag them differently, and net debt is flagged by sector.
- `long_term_debt` holds noncurrent totals only, so it does not overlap the current portion:
  `LongTermDebtNoncurrent`, then `LongTermDebtAndCapitalLeaseObligations` (which includes finance
  leases), then `LongTermNotesAndLoans`. Narrower lines (`LongTermLineOfCredit`,
  `SeniorLongTermNotes`, ...) can be one part of the debt: `long_term_debt_lines` adds up those on
  the balance sheet. Totals that include current maturities are in `long_term_debt_incl_current`
  (`LongTermDebt`, ...), and totals that also include short-term borrowings in `debt_total`.
- `cash`: `Cash` counts only on the balance sheet. In a note it can be one part of a line that
  bundles cash and short-term investments.
- `preferred_stock` prefers the carrying value (`PreferredStockIncludingAdditionalPaidInCapital`) over
  the par-only `PreferredStockValue`, which is zero or par at most filers and absent at some banks.

## How a concept gets its value (SEC)

`company_corpus/concepts/selection.py` generates one SQL statement from the dictionary. The F1 engine
and the open-data-store Recipe `fundamentals_concepts` run the same statement, so they give the same
values. Facts are stored as filed, by tag; which tag answers a concept is decided when reading, so
changing the dictionary changes no stored row.

For one concept and period, the candidates are the latest known value of each of its tags. They rank
by position (on the concept's statement, on another primary statement, elsewhere), then latest
filing, then dictionary order. Each result carries:

| Field | Meaning |
|---|---|
| `location` | `statement`, `note`, `inferred`, `unknown`, `derived` |
| `on_statement` | the line is on the concept's statement, in the filing's own or borrowed positions |
| `conflict` | Another candidate differs by more than `conflict_tolerance` (1%), or the value replaced an earlier one of the same tag by more than `restatement_tolerance` (50%). Both are in the dictionary file. |
| `review_status` | `reviewed` or `not_reviewed` |
| `dictionary_version` | The dictionary used |

### Facts filed before their period ended

A balance or a flow cannot be known before its period ends: a fact filed before (a wrong context, a
pro forma figure) is not a candidate. The rule is per concept in `concepts.json`:
`filed_before_period_end` is `drop` at the top level and `keep` only for `dividends_per_share` (a
dividend declared ahead of its quarter is a real fact). It applies to computed totals and sums too. A
fact is early when its filing day, read in New York (`DEFAULT_FILING_TZ`, a parameter of
`selection_sql`), is before its period end; a filing made on the period's last day is on time. F1 and
the store Recipe share the rule (one SQL statement), so a dropped fact leaves the concept empty for
that period unless an on-time candidate exists. One boundary differs: F1 reads the `filingDate`, the
store the acceptance instant, and EDGAR gives a filing accepted after 17:30 ET the next business
day's `filingDate`, so a filing accepted late just before a period end can be early in the store and
on time in F1. The restatement flag works per tag, before the concept rule: a dropped early vintage
can still set `conflict` on a later value of the same tag. A period whose rows were all early facts
of `drop` concepts has no row left, hence no summary; and an issuer whose only quarterly rows were
early has no quarters left, so its summaries switch to semi-annual when it has half-years. The
selection needs `known_at` as a TIMESTAMPTZ (`run_selection` refuses a naive timestamp), so the
DuckDB session `TimeZone` never moves the boundary.

Positions come from the SEC Financial Statement Data Sets (`company-corpus statements`). Those files
come out after the quarter ends, so the newest filings have no positions yet: such a filing borrows
those of the issuer's latest earlier filing, marked `inferred`. The value itself always comes from
the new filing. Each `xbrl --write` and `store-batches --write` (not with `--offline`) looks for a newly published file, so
the real positions replace the borrowed ones at the next run, without changing any stored fact.
Absent values stay empty: a line tagged only with a dimension or a share class is not in
`companyfacts`, and the run report counts it as `on_statement_no_value`.

Only financial reports count (`Config.store_report_forms`): 10-K, 10-Q, 20-F, 40-F, 6-K, 8-K and
their variants. A proxy statement tags net income in its pay table, sometimes at the wrong scale, and
would otherwise replace the 10-K's figure.

Known limits:

- Bulk files do not say which column a line is in, so a comparative period inherits its filing's
  location.
- F1 orders filings by filing date and the store by acceptance time: two filings of the same day can
  differ.
- `cash` of a bank is `CashAndDueFromBanks`, without interest-bearing deposits at other banks.
- A filing with two durations ending on one date can raise a spurious `conflict` in F1 (the store
  drops such a pair as `ambiguous_observation`).
- `liabilities` is computed when no total is filed: total liabilities and equity, minus total equity
  (else parent equity), minus the first mezzanine line, all from the balance sheet, marked `derived`.
  A filing newer than the bulk files gets no computed total until its real positions arrive.
  Parent `equity` and `net_income` are never computed, and metrics built on them stay empty.

## Derived metrics (single period)

Computed by `compute_derived`. Each metric is emitted only when **all** its
required inputs are present; additive *components* (current debt, short-term
investments, leases) default to 0. Monetary metrics carry the reporting currency;
ratios are `%` or `x`.

| Metric | Definition |
|---|---|
| `total_debt` | SEC: the largest of three builds, since a partial chain can only come out low. Noncurrent + current; a total with current maturities + the short-term debt beyond them; `debt_total`. Noncurrent is the larger of `long_term_debt` and `long_term_debt_lines`. Current counts only the current debt lines on the balance sheet when there are some, two lines of one value counting once; it is `debt_current_total`, or `lt_debt_current` + `short_term_debt` when it is absent, or the larger of the two when it is smaller than one of them. The total with current maturities is `long_term_debt_incl_current`, or with no noncurrent figure the larger of it and `debt_components`. When it is within 2% (`SAME_DEBT_TOLERANCE`) of noncurrent + current it is that same debt, and the short-term debt is not added again. The cost: a gross total within 2% of a build net of issuance costs hides a real short-term line of that size. EU and registers: `long_term_debt` + `lt_debt_current` + `short_term_debt`, with roll-up tags not double-counted |
| `total_debt_incl_leases` | total debt + finance & operating lease liabilities |
| `net_debt` | total debt − cash − short-term investments − **long-term investments** |
| `net_cash` | −`net_debt` (positive = net cash; offsets ST **and** LT investments) |
| `ebitda` | operating income + D&A |
| `free_cash_flow` | CFO − capex |
| `working_capital` | current assets − current liabilities |
| `tangible_book_value` | common equity (− preferred) − goodwill − intangibles |
| `nopat`, `invested_capital` | op. income × (1 − tax rate) ; total debt + total equity |
| `roic` | NOPAT ÷ invested capital (%) — **annual only** |
| `gross/operating/net/ebitda/fcf_margin` | the respective profit ÷ revenue (%) |
| `capex/rnd_intensity`, `sga_ratio` | the respective expense ÷ revenue (%) |
| `dividend_payout` | dividends paid ÷ net income (%) |
| `total_payout` | (dividends + buybacks) ÷ FCF (%) |
| `cash_conversion` | FCF ÷ net income (%) |
| `roe` | net income **to common** (− preferred div) ÷ equity (%) — **annual only** |
| `roa` | net income ÷ assets (%) — **annual only** |
| `effective_tax_rate` | income tax ÷ pretax income (%) |
| `debt_to_equity`, `debt_to_assets` | total debt ÷ equity / assets (x) |
| `net_debt_to_ebitda` | net debt ÷ EBITDA (x) — **annual only** |
| `interest_coverage` | operating income ÷ interest expense (x) |
| `cfo_to_debt`, `fcf_to_debt` | CFO / FCF ÷ total debt (x) — **annual only** |
| `current/quick/cash_ratio` | liquidity ratios (x) |
| `asset_turnover` | revenue ÷ assets (x) — **annual only** |
| `dso/dio/dpo` | receivables / inventory / payables ÷ (revenue or COGS ÷ 365) — **annual only** |
| `ccc` | DSO + DIO − DPO (days) — **annual only** |
| `book_value_per_share`, `tangible_book_value_per_share` | (common equity / TBV) ÷ shares |

**Net debt / net cash.** `net_debt` subtracts **long-term marketable securities**
(`MarketableSecuritiesNoncurrent`, ...) as well as cash and short-term investments —
without this it overstated net debt for cash-rich issuers (Apple read +$44B net
debt when it holds ~$78B of long-term securities and is ~$34B **net cash**). `net_cash`
is the positive mirror for readability. **ROIC** uses invested capital = total debt +
total equity (incl. NCI), with **no cash netting** — keeping the ratio sane for
cash-rich firms (validated against published values: Costco ~22%, Microsoft ~27%,
Walmart ~15%). **ROE** and book value are on a **common-equity** basis (net of
preferred), matching the per-share book values banks report (verified vs BofA / JPMorgan).

### Overlap-aware aggregation (no double counting)

The first two overlaps apply only to the EU and register path (`debt_rule="tags"`), whose chains
mix roll-ups and components, so the resolved source tag decides. On the SEC path each concept has one
meaning: `LongTermDebt` is in `long_term_debt_incl_current` and `DebtCurrent` in `debt_current_total`, and
`total_debt` is built from the concepts (see the table above). Three FASB-confirmed overlaps:

- **`LongTermDebt`** is the roll-up (`= LongTermDebtCurrent + LongTermDebtNoncurrent`).
  When it is the resolved tag, the current portion is *not* added again (EU and registers).
- **`DebtCurrent`** already includes current maturities of long-term debt; when it
  backs `short_term_debt`, the separate current portion is *not* added again (EU and registers).
- **`CashCashEquivalentsAndShortTermInvestments`** already includes short-term
  investments; when it backs `cash` (on the SEC path, when `cash_and_st_investments` stands in for
  `cash`), STI is *not* subtracted/added again in `net_debt` / `cash_ratio`.

### Guards

A ratio with a non-meaningful denominator is omitted rather than emitting a
misleading number: `roe` / `debt_to_equity` when equity ≤ 0, `effective_tax_rate`
when pretax income ≤ 0. (Several large issuers carry negative book equity from
buybacks.) `roe`/`roa` are stock/flow ratios and are therefore **annual only** in
the single-period block — their sub-annual signal is carried by the TTM versions.

## Financial-sector flagging

Banks and insurers (SIC 6000–6499) have no classified balance sheet, no COGS, and
treat interest and cash as operating items, so metrics like EBITDA, net debt,
coverage and liquidity ratios are low-information for them. **Nothing is dropped** —
every derived metric carries a boolean `sector_relevant` (False for the
sector-sensitive set when the issuer is financial). This keeps the corpus complete
and consistent (a metric's presence never depends on whether the SIC was fetched);
a consumer that wants to exclude bank EBITDA can filter `sector_relevant == False`.
`sic` and `is_financial` are surfaced on every output row.

## Derived quarters

Since 0.3.0. Many issuers file their cash-flow statements **year to date**: 3, 6 and 9 months in
the 10-Qs, then the fiscal year in the 10-K (Alphabet, Meta and Oracle among them). Only Q1 is then
a filed quarter. `concepts.quarters.with_derived_quarters` adds the standalone quarters to a
selection, as ordinary `quarterly` rows marked derived; the store is never touched (a producer
publishes what was filed, anything computed is a reader's recipe).

| Quarter | Formula (`derived`) | Inputs |
|---|---|---|
| Q2 | `6M - Q1` | 6-month YTD (`semiannual`) minus Q1 (`quarterly`) |
| Q3 | `9M - 6M` | 9-month YTD (`ytd9`) minus 6-month YTD |
| Q4 | `FY - 9M` | fiscal year (`annual`) minus 9-month YTD |

Rules:

- **Filed wins.** A quarter filed for the concept, entity, basis, source and period end is never
  derived or replaced.
- **Both inputs or nothing**, same concept, entity, basis, source, unit and fiscal-year start;
  never chained from another derived quarter. The derived span must fall in the quarterly band
  (81 to 101 days), else nothing (`quarters:not_a_quarter`).
- **Odd results stay visible**: a negative difference is kept, marked derived.
- **Eligible concepts**: money flows only (`kind == "money"` and not instant): `revenue`,
  `cost_of_revenue`, `gross_profit`, `operating_income`, `pretax_income`, `pretax_domestic`,
  `pretax_foreign`, `income_tax`, `net_income`, `net_income_consolidated`, `net_income_nci`,
  `interest_expense`,
  `interest_net`, `rnd_expense`, `sga_expense`, `dep_amort`, `stock_comp`, `cfo`, `cfi`, `cff`,
  `capex`, `acquisitions_net`, `asset_sale_proceeds`, `buybacks`, `dividends_paid`,
  `preferred_dividends`, `debt_proceeds`, `debt_repayments`, `finance_lease_principal`.
  Per-share and share-count concepts (6M EPS minus Q1 EPS is not the quarter's EPS) and balances
  are never derived.
- **Q4 for every issuer.** Almost nobody files a standalone Q4 (Microsoft and Amazon file 9M and
  FY too), so `FY - 9M` is derived for every issuer and every eligible concept: each fiscal
  year-end gets a quarterly summary.

**Marker.** A derived row copies its later input and sets `frequency = "quarterly"`,
`period_start` (the day after the earlier input ends), `value`, `location = "derived"`,
`original_tag = "derived:<later input's tag>"` and `derived` (the formula); `conflict` is either
input's, `on_statement` both inputs'. Filed rows carry no `derived` key. In a `PeriodSummary`,
every value has `derived` (the formula, or null for a filed value) and its `tag` is the later
input's filed tag; `metrics_from_derived(summary)` names the point-in-time metrics that rest on a
derived value (free cash flow, FCF margin, capex intensity...).

**Point in time.** A derived quarter exists only once both inputs are public. The row compares
the two inputs' selected vintages: its `known_at`, `filing_ref` and `source_doc_id` are those of
the vintage known last. The selection keeps the latest vintage of each value, so that is usually a
re-filing (next year's comparative, possibly a 10-Q for a Q4) rather than the first publication,
as for a filed row. A summary's publication date and accession take a derived value at the later of
its two inputs' **first** filings (the `fundamentals` vintages on the store path, the
companyfacts points on F1), so a derived Q4 is dated by the 10-K, and a period whose duration
values are all derived is dated by them alone (an earlier filing that only holds the period's
balances does not date it). With an `as_of`, run the selection on the vintages and positions
known at that instant and derive from that selection: the day before a 10-Q is accepted its
quarter does not exist, the day after it does (`tests/live/test_derived_quarters_real.py`, on
Alphabet's Q3 2024).

**6-month rows.** For a period where a quarter ends, the issuer's 6-month YTD (`semiannual`) rows
are derivation inputs, like `ytd9`: `hidden_semiannual(rows)` returns those period ends, and a
store reader such as the MCP leaves those rows out of its default output (`companies_facts`
still shows them). Decided per fiscal year: an issuer that files no quarters (a semi-annual reporter) keeps its 6-month rows and
summaries.

**Paths.** `build_summaries` / `attach_ttm` (F1) and the store readers' `summaries_from_rows`
share one computation (`concepts.summaries.with_quarters`). Validated on the recorded real
issuers (Alphabet, Meta, Oracle from FY2022: every derived quarter equals the hand subtraction of
the filed values; Microsoft and Amazon: filed values unchanged and, from FY2022 on, the additions
are derived Q4s; Amazon earlier also gains a few derived Q2/Q3, from cash-flow lines it filed year
to date in 2012 and 2015).

Checked against the filing text on 2026-11-27: each year-to-date capex input of three pinned
years was found in the cash-flow statement of the filing that first reported it (primary document
converted with `company_corpus.live.documents`; USD millions).

| Issuer | Period (input) | Filed | Document | Found | Line |
|---|---|---|---|---|---|
| Alphabet | 2024-03-31 (3M) | 12,012 | 10-Q 0001652044-24-000053 | yes | Purchases of property and equipment (12,012) |
| Alphabet | 2024-06-30 (6M) | 25,198 | 10-Q 0001652044-24-000079 | yes | Purchases of property and equipment (25,198) |
| Alphabet | 2024-09-30 (9M) | 38,259 | 10-Q 0001652044-24-000118 | yes | Purchases of property and equipment (38,259) |
| Alphabet | 2024-12-31 (FY) | 52,535 | 10-K 0001652044-25-000014 | yes | Purchases of property and equipment (52,535) |
| Meta | 2024-03-31 (3M) | 6,400 | 10-Q 0001326801-24-000049 | yes | Purchases of property and equipment, net (6,400) |
| Meta | 2024-06-30 (6M) | 14,573 | 10-Q 0001326801-24-000069 | yes | Purchases of property and equipment, net (14,573) |
| Meta | 2024-09-30 (9M) | 22,831 | 10-Q 0001326801-24-000081 | yes | Purchases of property and equipment, net (22,831) |
| Meta | 2024-12-31 (FY) | 37,256 | 10-K 0001326801-25-000017 | yes | Purchases of property and equipment (37,256) |
| Oracle | 2024-08-31 (3M) | 2,303 | 10-Q 0000950170-24-104905 | yes | Capital expenditures (2,303) |
| Oracle | 2024-11-30 (6M) | 6,273 | 10-Q 0000950170-24-134973 | yes | Capital expenditures (6,273) |
| Oracle | 2025-02-28 (9M) | 12,135 | 10-Q 0000950170-25-037143 | yes | Capital expenditures (12,135) |
| Oracle | 2025-05-31 (FY) | 21,215 | 10-K 0000950170-25-087926 | yes | Capital expenditures (21,215) |

Hence the derived capex quarters: Alphabet 2024 Q2 13,186, Q3 13,061, Q4 14,276; Meta 2024 Q2
8,173, Q3 8,258, Q4 14,425; Oracle FY2025 Q2 3,970, Q3 5,862, Q4 9,080 (pinned in
`tests/live/test_derived_quarters_real.py`).

**Migration (0.3.0).** Outputs gain derived rows; filed rows unchanged. F1 and store summaries
gain derived values (marked `derived`), a quarterly summary at each fiscal year-end, the metrics
computed on them, and quarterly TTM at Q2/Q3 of year-to-date filers. A consumer that treated
every `quarterly` value as filed must read `derived`; one that rebuilt Q4 as FY - 9M itself can
read the derived row instead.

## Trailing-twelve-month (TTM) ratios — Bloomberg-aligned

`compute_ttm_derived` + `attach_ttm_metrics` add a `ttm` block so quarterly ratios
match how Bloomberg reports them. Numerators are trailing-12-month flows; the
balance-sheet denominator for ROA / ROE / asset-turnover is a **2-point average**
of the current and year-ago period-end.

```
T12(X)  = sum of the 4 trailing standalone quarters ending at t
          (the unreported fiscal-year-end quarter is derived as Annual − the 9M YTD
          ending 80–100 days earlier; if two qualify, after a merger or a
          changed calendar, the latest)
AVG(B)  = (B at t + B one year earlier) / 2          # same frequency
PIT(B)  = B at t

roa_ttm              = T12(net_income)      / AVG(assets)            × 100
roe_ttm              = T12(net_income)      / AVG(equity)            × 100   # equity > 0
net/operating/gross/ebitda/fcf_margin_ttm   = T12(flow) / T12(revenue) × 100
asset_turnover_ttm   = T12(revenue)         / AVG(assets)            (x)
net_debt_to_ebitda_ttm = PIT(net_debt)      / T12(ebitda)           (x)
interest_coverage_ttm  = T12(ebitda)        / T12(interest_expense) (x)
```

Annual periods use the fiscal-year value as the flow window. A TTM metric is
omitted when its 4-quarter window is incomplete (early periods) or, for the
averaged metrics, when the year-ago balance is missing — never zero-filled. On the SEC
path the TTM flow series comes from the same selection as the summaries, so a tag
change across taxonomy vintages (e.g. `SalesRevenueNet` → `Revenues`) does not
suppress TTM; the EU and register paths union a concept's points across all its
fallback tags for the same reason.

**Derived quarters.** TTM reads derived Q2/Q3 as ordinary quarters, so a quarterly TTM at
Q2/Q3 of a year-to-date filer is computable; the fiscal year-end quarter stays decided by the Q4
rule (`ytd9` or `sum_of_quarters`), never by the derived Q4 row. See
[Derived quarters](#derived-quarters).

**Validation.** This reproduces Apple's published quarterly Return on Assets to
four decimal places: **32.5629** at 2025-12-27 and **34.9060** at 2026-03-28
(`tests/test_ttm.py`).

### Conventions / known limitations

- Negative-EBITDA leverage / coverage multiples (`net_debt_to_ebitda*`,
  `interest_coverage_ttm`) are emitted **as-is** (the multiple may be negative);
  consumers that want Bloomberg's "n.m." filter downstream.
- Returns/averages use reported period-end balances (no intra-period averaging
  beyond the 2-point TTM average).
- When a filer tags pretax income only as a geographic split
  (`...BeforeIncomeTaxesDomestic` + `...Foreign`, no consolidated line — e.g.
  McDonald's), `pretax_income` is reconstructed as their sum (the domestic-only tag
  is never used alone — it would make the tax rate = total tax ÷ US-only pretax).
- `gross_margin` falls back to `(revenue − cost_of_revenue) / revenue` when the
  filer reports cost but no explicit `GrossProfit` line (e.g. Walmart).
- `long_term_debt` captures `LongTermDebtAndCapitalLeaseObligations` (e.g. Comcast) as its
  second tag, after `LongTermDebtNoncurrent`; it bundles capitalised (finance) leases into the debt
  figure, which is debt-like and acceptable when the filer reports debt only in that
  combined form.
- Concepts absent from the IFRS table stay unmapped on the SEC path. Ingestion reads `us-gaap`,
  `ifrs-full` and `dei`, and `tags/ifrs_full.json` is a default table, so an IFRS-only filer is mapped
  for the concepts that table lists; the remaining limit is the set of concepts it does not (see
  `tags/ifrs_full.json`).

**Cross-checked live** against real filings for a 10-issuer basket (Apple, Microsoft,
Alphabet, Berkshire, Comcast, McDonald's, Walmart, Costco, JPMorgan, BofA): net
cash/debt, ROIC, margins, ROE, common-share book value, and the working-capital
cycle all match published values (e.g. Apple net cash ~$34B and CCC ~−71 days;
BofA/JPMorgan common BVPS & tangible BVPS to the dollar; Costco/Walmart CCC ~2–3 days).

## Output

`normalized_rows(entity_id, summary)` flattens a period into queryable rows for
`data/financials/<cik>.jsonl`, emitting `kind="reported"` (raw concepts, with their
source `tag`), `kind="derived"`, and `kind="derived_ttm"` rows. Every row carries
the 15-key unified identity/provenance base shared across all three pillars:
`entity_id` (CIK value), `id_scheme` (`"cik"`), `lei` (null), `country` (`"US"`),
`currency`, `period_end`, `fy`, `frequency`, `publication_date`, `source` (`"sec"`),
`form`, `accession`, `sic`, `is_financial`, `basis` (null); plus body keys `kind`,
`concept`, `label`, `value`, `unit`, `tag`; reported rows also carry `derived` (the formula
`6M - Q1`, `9M - 6M` or `FY - 9M` of a derived quarter, null for a filed value: every pillar's
reported rows have it, null outside the SEC path); derived/TTM rows also carry
`sector_relevant`. All three pillars (SEC, EU/ESEF, register) share this
identity/provenance schema; reported-row `label` values are canonical per concept
across all pillars. `render_summary_html` renders the reported, derived, and TTM
tables as a standalone HTML document.

### `leverage_basis` — register pillar only

Register-financials rows (`data/financials_register/`) carry an extra field,
`leverage_basis`, on the four leverage-derived concepts (`total_debt`,
`debt_to_equity`, `debt_to_assets`, `net_debt`). Its value is `"borrowings"` when
the register exposes real financial borrowings (BE/LU/SK/DK-ESEF), or
`"total_liabilities"` when it exposes only the aggregate liabilities balance as a
gearing proxy (NO/UK/EE/DK-FSA). Finland suppresses these rows entirely (maturity
split absent) and emits no `leverage_basis`. The field is absent on SEC and EU/ESEF
rows (where the XBRL taxonomy always provides explicit debt tags). Consumers
screening leverage across multiple registers must filter or bucket by this field —
`debt_to_equity` from a `"borrowings"` register is not directly comparable to one
from a `"total_liabilities"` register.

See `examples/04_xbrl_financials.py` for a runnable end-to-end example.
