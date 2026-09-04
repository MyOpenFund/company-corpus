# Financials — curated XBRL concepts, derived metrics & TTM ratios

`company_corpus/financials.py` distils the SEC `companyfacts` XBRL feed into one
**summary per actual reporting period** (annual / quarterly / semi-annual), each
carrying ~60 curated line items, a block of **derived metrics** (single-period),
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
`filed` for the period. Each value also records its source XBRL **tag** as
provenance.

**Per-period tag resolution.** A concept's value for a period comes from the
**highest-priority fallback tag that actually has a value for that period**, then
the latest-filed point within it. Filers switch tags across taxonomy vintages
(e.g. Microsoft's cost of revenue moved `CostOfRevenue` → `CostOfGoodsAndServicesSold`;
Alphabet's revenue `RevenueFromContractWithCustomerExcludingAssessedTax` → `Revenues`);
a first-tag-wins lookup would return the stale tag and silently drop the recent
period. Per-period resolution only adds coverage — it never changes an
already-resolved value.

Monetary facts are filtered to the issuer's dominant reporting currency, so a
convenience translation (e.g. a USD value alongside a primary EUR one) never gets
summed or divided as if it were the functional currency.

## Curated concepts

~60 reported line items across income statement, per-share, cash flow, and balance
sheet, each with fallback XBRL tags in priority order (see `CONCEPTS` in
`financials.py`). Beyond the core lines (`revenue`, `operating_income`,
`net_income`, `cfo`, `capex`, `assets`, `equity`, `long_term_debt`, `cash`) the set
includes `long_term_investments` (noncurrent marketable securities),
`preferred_stock` (carrying value), `noncontrolling_interest`, `equity_total`,
`net_income_nci`, `acquisitions_net`, `debt_proceeds`/`debt_repayments`,
`restricted_cash`, `retained_earnings`, `treasury_stock`, `aoci`, and
`pension_obligations`.

`equity` is **parent-only** (`StockholdersEquity`); the NCI-inclusive figure lives
in `equity_total`, so ROE / book value never divide parent income by parent+NCI
equity. `preferred_stock` prioritises the **carrying-value** tag
(`PreferredStockIncludingAdditionalPaidInCapital`) over the par-only
`PreferredStockValue` (which is 0/par at most filers and absent at some banks).

## Derived metrics (single period)

Computed by `compute_derived`. Each metric is emitted only when **all** its
required inputs are present; additive *components* (current debt, short-term
investments, leases) default to 0. Monetary metrics carry the reporting currency;
ratios are `%` or `x`.

| Metric | Definition |
|---|---|
| `total_debt` | long-term debt + current portion + short-term borrowings |
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

Three FASB-confirmed concept overlaps are handled by branching on the resolved
source tag:

- **`LongTermDebt`** is the roll-up (`= LongTermDebtCurrent + LongTermDebtNoncurrent`).
  When it is the resolved tag, the current portion is *not* added again.
- **`DebtCurrent`** already includes current maturities of long-term debt; when it
  backs `short_term_debt`, the separate current portion is *not* added again.
- **`CashCashEquivalentsAndShortTermInvestments`** already includes short-term
  investments; when it backs `cash`, STI is *not* subtracted/added again in
  `net_debt` / `cash_ratio`.

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

## Trailing-twelve-month (TTM) ratios — Bloomberg-aligned

`compute_ttm_derived` + `attach_ttm_metrics` add a `ttm` block so quarterly ratios
match how Bloomberg reports them. Numerators are trailing-12-month flows; the
balance-sheet denominator for ROA / ROE / asset-turnover is a **2-point average**
of the current and year-ago period-end.

```
T12(X)  = sum of the 4 trailing standalone quarters ending at t
          (the unreported fiscal-year-end quarter is derived as Annual − 9M YTD)
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
averaged metrics, when the year-ago balance is missing — never zero-filled. The
TTM flow series unions a concept's points across all its fallback tags, so a tag
change across taxonomy vintages (e.g. `SalesRevenueNet` → `Revenues`) does not
suppress TTM.

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
- `long_term_debt` captures `LongTermDebtAndCapitalLeaseObligations` (e.g. Comcast)
  as a last-resort fallback; it bundles capitalised (finance) leases into the debt
  figure, which is debt-like and acceptable when the filer reports debt only in that
  combined form.
- Full IFRS (`ifrs-full`) concept mapping is deferred to the international pillar;
  an IFRS-only filer currently maps just `net_income` (via `ProfitLoss`).

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
`concept`, `label`, `value`, `unit`, `tag`; derived/TTM rows also carry
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
