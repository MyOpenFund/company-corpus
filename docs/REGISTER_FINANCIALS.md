# Register Financials — statutory accounts from national business registers

`company_corpus/registers/` ingests **open-data statutory accounts** from national
business registers and writes them in the same curated schema as the SEC and EU pillars.
This document covers the purpose, source, schema, honest caveats, and CLI for eight
registers: **Norway's Brønnøysund Register Centre (Brreg)**, **UK Companies House**,
**Belgium's BNB Central Balance Sheet Office (CBSO)**, **Luxembourg's LBR/STATEC
Centrale des bilans**, **Finland's PRH avoindata XBRL platform**, **Denmark's
Erhvervsstyrelsen / Virk "Regnskaber"**, **Estonia's Äriregister (RIK
avaandmed)**, and **Slovakia's Register účtovných závierok (registeruz.sk)**.

See also: [`FINANCIALS.md`](FINANCIALS.md) (the SEC/US-GAAP pillar that defines the
shared schema and derived-metrics engine) and [`EU_FINANCIALS.md`](EU_FINANCIALS.md)
(the ESEF/IFRS pillar for listed EU issuers).

## Purpose

The SEC and EU ESEF pillars cover **listed issuers** — but the credit and private-company
universe is dominated by **non-listed entities** that never file an annual report with an
exchange or regulator, and therefore never appear on SEC EDGAR or filings.xbrl.org.
National business registers are the primary structured source for this universe: in most
jurisdictions, every legal entity above a size threshold is required to file annual
statutory accounts at the register, and those accounts are increasingly available as
open data.

A second motivation is **depth of history**: ESEF mandates structured iXBRL only from
2020 onwards, whereas national registers often hold statutory filings going back a decade
or more. For Norwegian entities, Brreg's open JSON endpoint typically returns 5–10 years
of history, well beyond the ESEF cap.

The register output lives in a **separate directory** (`data/financials_register/`) and
is **never merged** with the ESEF consolidated output (`data/financials_eu/`). The two
pillars serve different universes and different GAAP regimes; keeping them separate
avoids silent cross-contamination in analytical tables.

## Source — Brreg Regnskapsregisteret (Norway)

```
GET https://data.brreg.no/regnskapsregisteret/regnskap/{orgnr}
```

**No API key required.** The endpoint is public and returns a JSON **list** of annual
account entries for the given `orgnr` (Norwegian organisation number, 9 digits). Each
entry contains:

| JSON field | Meaning |
|---|---|
| `regnskapstype` | `SELSKAP` (standalone legal entity) or `KONSERN` (consolidated group) |
| `valuta` | Reporting currency (typically `NOK`) |
| `regnskapsperiode.tilDato` | Period-end date (ISO-8601 date string) |
| `resultatregnskapResultat` | Income-statement block |
| `eiendeler` | Assets block |
| `egenkapitalGjeld` | Equity-and-liabilities block |

The engine flattens the three account blocks recursively by field name and maps the
resulting leaves to the curated concept keys (`concepts_no.py`). There is **no XBRL or
Arelle** involved: Brreg's JSON has named numeric fields, so the mapping is a direct
field-to-key lookup — a much simpler path than the ESEF/SEC XBRL pipelines.

A `404` or a network error returns an empty list; the entry is recorded as
`no-financials` in the coverage report and never silently dropped.

## Schema — `data/financials_register/<orgnr>.jsonl`

Output follows the same per-period row model as the SEC and EU pillars
(see [`FINANCIALS.md`](FINANCIALS.md) for the full definition of curated concepts and
derived metrics). Each row carries:

| Column | Value |
|---|---|
| `entity_id` | Org number (9-digit string) |
| `lei` | GLEIF LEI if supplied or resolved; `null` otherwise |
| `country` | `"NO"` |
| `source` | `"brreg"` |
| `basis` | `"company"` (from `SELSKAP`) or `"consolidated"` (from `KONSERN`) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` (Brreg holds only annual statutory accounts) |
| `currency` | Reporting currency from `valuta` (typically `"NOK"`) |
| `period_end` | ISO-8601 date string |
| `publication_date` | `null` (Brreg does not expose a filing date) |

Beyond the identity/period columns, each row is one of **two** kinds: `kind="reported"`
(the mapped curated concepts, each carrying its source Brreg field name as `tag`) and
`kind="derived"` (single-period metrics from the shared `compute_derived` engine). **No
`derived_ttm` rows are written**: Brreg holds annual accounts only, so the
trailing-twelve-month block is inert and never attached for register inputs.

Register inputs carry **no depreciation/amortization and no cash-flow statement**, so the
engine emits a strict *subset* of its full derived block — for a complete filer
(e.g. Equinor) exactly these:

- **Aggregates:** `total_debt`, `net_debt`, `net_cash`, `working_capital`,
  `nopat`, `invested_capital`
- **Margins:** `operating_margin`, `net_margin`
- **Returns / tax:** `roe`, `roa`, `roic`, `effective_tax_rate`
- **Leverage / coverage:** `debt_to_equity`, `debt_to_assets`, `interest_coverage`
- **Liquidity:** `current_ratio`, `quick_ratio`, `cash_ratio`
- **Efficiency:** `asset_turnover`, `dso`

A metric whose specific inputs are missing for a given period is simply omitted (e.g.
`interest_coverage` needs `annenRentekostnad`; `dso` needs `sumFordringer`). There is in
particular **no `ebitda`** (no D&A), **no `free_cash_flow`, `cfo_to_debt` or
`fcf_to_debt`** (no cash-flow statement) and **no `net_debt_to_ebitda`** — these are
never produced for register rows.

There is also **no `tangible_book_value`**: Brreg aggregates intangibles into
`sumAnleggsmidler` and exposes no goodwill/intangibles breakdown, so true tangible book
value cannot be derived — the engine's TBV would collapse to `equity` and silently
overstate it for any obligor carrying intangibles. Plain book value remains available as
the reported `equity` line.

A coverage report is written to `data/reports/register_coverage_brreg.jsonl` for every
entity processed: `status="ok"` with a period count, or `"no-financials"` / `"unresolved"`.

## The `basis` field — statutory entity vs. consolidated group

This distinction matters for credit analysis and is explicit in every row:

- **`basis="company"`** (from `SELSKAP`): the **legal entity's own standalone accounts**
  under Norwegian GAAP (N-GAAP). For a holding company, these are the **parent entity's
  accounts alone** — not the group. Revenue, assets, and income reflect only the legal
  entity, not its subsidiaries. This is often the correct unit of analysis for credit
  (the obligor is the legal entity, not the economic group).

- **`basis="consolidated"`** (from `KONSERN`): the **group consolidated accounts**,
  also under N-GAAP. This is the economic group, analogous in concept to the ESEF
  consolidated output — but computed under Norwegian GAAP rather than IFRS, and sourced
  from the register rather than the ESEF mandatory filing.

Both are retained when Brreg provides them, clearly labelled. A query that wants only
group-level financials should filter `basis="consolidated"`; a query targeting the legal
obligor should use `basis="company"`.

Note that `basis="consolidated"` from Brreg is **not the same as** the ESEF consolidated
rows in `data/financials_eu/`: the GAAP differs (N-GAAP vs. IFRS), the source differs
(register vs. ESEF filing), and the concepts mapped differ in granularity. Never merge
the two without an explicit accounting-regime reconciliation.

## Honest caveats

### Leverage is liabilities-based

Brreg provides total-liabilities figures (`sumGjeld`, `sumLangsiktigGjeld`,
`sumKortsiktigGjeld`) but **not pure financial borrowings** (bonds, bank loans,
leases). The mapping therefore approximates:

```
short_term_debt  ← sumKortsiktigGjeld  (all current liabilities)
long_term_debt   ← sumLangsiktigGjeld  (all non-current liabilities)
total_debt       ≈ total liabilities   (not pure financial debt)
```

When a filer has no non-current liabilities, Brreg omits the `sumLangsiktigGjeld` leaf
entirely (`langsiktigGjeld: {}`). Because the engine gates `total_debt` — and therefore
every gearing metric — on `long_term_debt` being present, `long_term_debt` is in that
case **synthesized** as total − current liabilities (`sumGjeld − sumKortsiktigGjeld`,
recorded with a `derived` tag) so that gearing stays available for the small/private
filers this pillar targets. When the leaf is present it is used directly, with no
synthesis.

As a result, **every derived metric built on `total_debt` inherits this
total-liabilities basis**:

- `debt_to_equity` and `debt_to_assets` are **liabilities-based gearing**, not
  pure-borrowings ratios.
- `net_debt` here is **total liabilities − cash** (NOT financial net debt), and
  `net_cash` is its mirror. `invested_capital` (= total liabilities + equity) and
  `roic` (NOPAT / invested capital) therefore also rest on the total-liabilities basis.
  Read these as liabilities-based, not borrowings-based, figures.
- `interest_coverage` (operating income / interest) is an **approximation**: Brreg's
  only gross-interest field is `annenRentekostnad`, and intra-group interest
  (`rentekostnadSammeKonsern`) is not added in, so coverage **excludes intra-group
  interest**. (The net `sumFinanskostnad` is deliberately *not* used — it is a net
  financial figure, not gross interest.)
- The `net_debt_to_ebitda`, `cfo_to_debt` and `fcf_to_debt` ratios are **not emitted at
  all** for register rows (no EBITDA and no cash-flow statement — see the schema section).
- This is coarser than the ESEF/SEC pillars (which map tagged debt line items from XBRL)
  but is the best available from the register's structured data.
- The source field `tag` in each reported row records which Brreg field was used, so
  downstream consumers can inspect the mapping.

For the credit and private-company universe — where Brreg is typically the **only**
structured source — this approximation is accepted as-is. Cross-checking against the
entity's actual credit agreements or audited notes is the appropriate next step for
individual names.

### Annual accounts only

Brreg holds statutory annual accounts. There are no quarterly or semi-annual periods.
`frequency` is always `"annual"`.

### Norwegian GAAP (N-GAAP), not IFRS

The statutory accounts filed at Brreg follow N-GAAP (or IFRS as adopted by Norwegian
law for large listed entities). Concept definitions differ from IFRS in edge cases
(e.g. treatment of deferred tax, pension, minority interest). The curated mapping
(`concepts_no.py`) maps the Brreg fields as closely as possible to the shared schema
keys, but users comparing register rows to ESEF rows for the same entity should be
aware of these GAAP differences.

## Identity resolution

Two input modes, mutually exclusive:

- **`--orgnrs`**: comma-separated 9-digit Norwegian org numbers. Used directly, no
  external lookup required.
- **`--leis`**: comma-separated GLEIF LEIs. For each LEI, the engine calls
  `GET https://api.gleif.org/api/v1/lei-records/{lei}` and extracts the
  `entity.registeredAs` field. This is accepted as the orgnr **only when**
  `entity.legalAddress.country == "NO"` — there is no guess, no fuzzy match. A LEI
  from a non-Norwegian jurisdiction returns `status="unresolved"` in the coverage
  report.

In both modes, the resolved `lei` (if available) is carried through to every output row.
An entity that could not be resolved is recorded in the coverage report and skipped —
never a silent drop.

## CLI usage

```
# dry-run (default): print summary, nothing written
company-corpus register-financials --orgnrs 923609016,974760673

# write data/financials_register/<orgnr>.jsonl + coverage report
company-corpus register-financials --orgnrs 923609016,974760673 --write

# resolve from LEIs (GLEIF lookup, Norway only)
company-corpus register-financials --leis 5493001KJTIIGC8Y1R12 --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints the
entity and period count without touching disk. `--orgnrs` and `--leis` are mutually
exclusive.

---

## Source — UK Companies House statutory accounts (iXBRL)

UK statutory accounts are filed with **Companies House** as **iXBRL** (inline XBRL)
documents under **FRC taxonomies**: FRS 105 for micro-entities, FRS 102 for small and
medium companies (the dominant volume by count), and IFRS for listed or large companies.
Each filing is a bare `.html` file carrying inline XBRL tags in the FRC namespace.

The engine parses these files with **Arelle** — the same bare-`.html` loader already
used by the EU Tier B pipeline (`oim_from_ch_html` → `flatten_oim_json`). The OIM-JSON
fact format produced by Arelle is flattened by `flatten_oim_json` and mapped to curated
concepts by `map_ch_facts` (`concepts_uk.py`). No new XBRL infrastructure is needed: the
EU Tier B path is reused without duplication.

### Two acquisition paths

| Path | Key required | Status |
|---|---|---|
| **Accounts Data Product** (bulk monthly ZIP) | None | Built — `--ch-bulk` |
| **Companies House REST API** (targeted, per CH number) | Free developer key | Deferred — next PR |

The **Accounts Data Product** is a public bulk download. Companies House publishes one
ZIP per monthly cut, containing every set of accounts filed during that month as
individual `.html` iXBRL files. No API key or authentication is required. The
`--ch-bulk` flag takes the local path to a downloaded ZIP and processes every filing
inside it in a single pass (with an optional `--limit N` cap for bounded test runs).

The **targeted REST API** accepts a CH number and returns that company's filing history.
It requires a free developer key and is better suited to named-entity workflows where
processing the full bulk extract is not needed. Targeted-API support is deferred to a
follow-up PR; the `--ch-bulk` path does not filter by company — it processes the entire
ZIP.

## The balance-sheet-primary reality

The UK private-company universe is **overwhelmingly micro and small companies** filing
**balance-sheet-only** statutory accounts. In a calibration sample of 5,326 files drawn
from the Accounts Data Product:

| Measure | Approximate share |
|---|---|
| Any balance-sheet data (net assets / equity tagged) | ~72% |
| Revenue (`TurnoverRevenue`) tagged | ~1.3% |
| Dormant / nil filings | ~28% |

The practical consequence: this register is a **balance-sheet register, not a P&L
register**. For the overwhelming majority of filers the useful outputs are **equity, net
assets, leverage, and liquidity** — not revenue, operating income, or margin ratios.
P&L concepts (`revenue`, `gross_profit`, `operating_income`, `net_income`) are available
only for the minority that file **full accounts** rather than abridged or micro-entity
accounts.

Do not use this register to rank or screen UK private companies by revenue: approximately
98.7% of filings carry no tagged revenue and would silently drop out of any such query.
Balance-sheet scoring and equity-based ranking are the intended use case.

## Schema — `data/financials_register/<ch_number>.jsonl`

Output follows the same per-period row model as the NO register and the SEC/EU pillars
(see [`FINANCIALS.md`](FINANCIALS.md) for the full curated-concepts definition):

| Column | Value |
|---|---|
| `entity_id` | CH number (string; verbatim — leading zeros and SC/NI/OC prefixes preserved) |
| `lei` | `null` (LEI resolution deferred — see Identity section below) |
| `country` | `"GB"` |
| `source` | `"companies_house"` |
| `basis` | `"company"` (statutory legal entity; consolidated detection deferred) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` |
| `currency` | Reporting currency from the iXBRL unit (typically `"GBP"`) |
| `period_end` | ISO-8601 date string (the latest end date seen in the iXBRL filing) |
| `publication_date` | `null` (filing date not extracted from the bulk product) |

Each row is `kind="reported"` (directly-tagged curated concepts, each carrying its FRC
local name as `tag`) or `kind="derived"` (single-period metrics from the shared
`compute_derived` engine). No `derived_ttm` rows: the bulk product contains one period
per filing.

Because most UK filings carry only balance-sheet data, the derived block is a strict
subset of the engine's full output. For a **complete filer** (revenue + full balance
sheet both tagged):

- **Aggregates:** `total_debt`, `net_debt`, `net_cash`, `working_capital`,
  `invested_capital`
- **Margins:** `operating_margin`, `net_margin`
- **Returns:** `roe`, `roa`
- **Leverage / liquidity:** `debt_to_equity`, `debt_to_assets`, `current_ratio`,
  `quick_ratio`, `cash_ratio`

For the **majority of balance-sheet-only filers**, every metric that requires revenue or
income is absent. There is no `ebitda` (no D&A in the iXBRL schema), no
`free_cash_flow`, `cfo_to_debt`, or `fcf_to_debt` (no cash-flow statement), no
`net_debt_to_ebitda`, and no `tangible_book_value` (suppressed — FRC filings carry no
goodwill/intangibles breakdown, so the engine's TBV would collapse to equity and
silently overstate it for any filer carrying intangibles; suppressed via the shared
`_SUPPRESSED_CONCEPTS` filter, the same mechanism as the NO register).

A coverage report is written to `data/reports/register_coverage_companies_house.jsonl`
for every filing processed: `status="ok"` with a period count, `"no-financials"` (no
usable iXBRL facts or empty values after gating), `"unbalanced"` (primary gate rejected
the filing), or `"source-error"` (dead source or parse failure). When items were suppressed by the confidence
gate, a `suppressed` list records each key name and the reason.

## The confidence gate — no false data

**Governing principle (from `concepts_uk.py`):** a number known to be wrong must never
be emitted; a missing number is strictly better than a wrong one. UK iXBRL filings carry
no universally-tagged `TotalAssets` or `TotalDebt` field — totals must be **derived from
structural anchors**, and every derivation is gated: if the anchor does not confirm the
result, the derived value is suppressed and the reason is recorded. The engine never
defaults a missing balance-sheet item to zero.

### Primary gate: `NetAssetsLiabilities == Equity`

Both `NetAssetsLiabilities` and `Equity` are independently tagged in the vast majority
of balance-sheet filings. If they disagree beyond a tolerance of
`max(2 GBP, 0.5% of the larger figure)`, the **entire filing is rejected**:
`status="unbalanced"`, no values emitted at all. A balance sheet that does not close is
untrustworthy by construction; partial values from such a filing would actively mislead a
downstream user.

### Anchor check: `TALCL == FixedAssets + NetCurrentAssets`

When `FixedAssets` is tagged, `TotalAssetsLessCurrentLiabilities` must equal
`FixedAssets + NetCurrentAssetsLiabilities`. A mismatch proves the balance-sheet inputs
are internally inconsistent. On mismatch, **every derived balance-sheet item**
(`assets`, `liabilities`, `liabilities_current`, `short_term_debt`, `long_term_debt`) is
suppressed — the directly-tagged P&L lines and equity / cash still stand.
The mismatch reason is recorded per key in the coverage `suppressed` list.

### The `assets` derivation — why TALCL, not `FixedAssets + CurrentAssets`

```
assets = TotalAssetsLessCurrentLiabilities + current_liabilities
```

Not `FixedAssets + CurrentAssets`. The reason: `FixedAssets` is frequently **untagged**
in micro and small-company filings — dimensioned away when the company holds no fixed
assets, or reported under sub-components that the taxonomy does not roll up into the
`FixedAssets` concept. Using `FixedAssets + CurrentAssets` in those cases would
**silently understate** total assets (a wrong number). The TALCL anchor is tagged
consistently even when fixed assets are absent, making it the robust derivation path.

### Atomic liability block — no partial debt

The derived liability / debt block is emitted **all-or-nothing**:

```
current_liabilities  =  CurrentAssets − NetCurrentAssetsLiabilities
long_term_debt       =  TotalAssetsLessCurrentLiabilities − NetAssets
short_term_debt      =  current_liabilities   (mirrors current liabilities; total-liabilities basis)
assets               =  TotalAssetsLessCurrentLiabilities + current_liabilities
liabilities          =  current_liabilities + long_term_debt
```

If either the current-liabilities inputs (`CurrentAssets`, `NetCurrentAssetsLiabilities`)
or the long-term input (`TotalAssetsLessCurrentLiabilities`) is absent, the whole block
is withheld. Emitting only one half would silently understate `total_debt` (the engine
computes it as `long_term_debt + short_term_debt`) — a wrong number is worse than none.

### Leverage is total-liabilities-based

As with the NO register, `short_term_debt` mirrors current liabilities and
`long_term_debt` mirrors non-current liabilities: `total_debt` equals **total
liabilities**, not pure financial borrowings (bonds, bank debt, leases). All gearing
metrics (`debt_to_equity`, `debt_to_assets`, `net_debt`, `net_cash`) inherit this
total-liabilities basis. FRC-tagged filings do not consistently expose a pure borrowings
breakdown, so this is the best available from the register.

All leverage rows (`debt_to_equity`, `total_debt`, `net_debt`, `debt_to_assets`) carry a
`leverage_basis` field (`"total_liabilities"` for UK/NO; `"borrowings"` for
borrowings-based registers such as BE) so registers are never compared blindly.

### Coverage yields to correctness

Suppressed items and their reasons are always recorded in the coverage report. The
practical distribution in a typical bulk batch:

- ~72% of filings yield at least `equity` rows
- Derived balance-sheet items (`assets`, `liabilities`) are available where TALCL and
  NetCurrentAssetsLiabilities are both tagged
- Revenue and P&L are present in ~1.3% of filings
- A filing that fails the primary gate is counted as `unbalanced` (not `no-financials`),
  so the distinction is queryable in the coverage report

## The `basis` field — UK

All UK rows carry `basis="company"`. UK group consolidated accounts are filed in the
same iXBRL format as standalone legal-entity accounts, but the engine does not currently
distinguish the two (the FRC taxonomy does not mandate a consistent basis-of-consolidation
tag). Consolidated detection — analogous to Brreg's `KONSERN` / `SELSKAP` distinction —
is deferred to a follow-up PR.

Note that `basis="company"` rows from Companies House and `basis="consolidated"` rows
from the EU ESEF pillar (`data/financials_eu/`) are already separated by output
directory. They must **not** be merged without an explicit GAAP and scope reconciliation:
FRS 102 / FRS 105 differs materially from IFRS on pension, deferred tax, and leases.

## Identity — CH number and LEI

**CH number (entity_id):** The `entity_id` is the CH number exactly as it appears in the
bulk ZIP filename — **no digit-stripping, no prefix removal, no normalisation**. Scottish
(`SC`), Northern Irish (`NI`), and Limited Liability Partnership (`OC`) prefixes are
preserved verbatim, as are leading zeros on numeric CH numbers. Stripping or normalising
would conflate distinct companies or break lookups against the Companies House register.

**LEI resolution (deferred):** The bulk product does not include LEI data. All rows
currently carry `lei=null`. A future targeted-API PR will add optional LEI lookup via
GLEIF: the `registeredAs` field is accepted as a CH number only when
`entity.legalAddress.country == "GB"` — the same country guard as the NO register's
`"NO"` filter, so a LEI registered in a different jurisdiction never silently resolves to
a GB CH number.

## CLI usage — UK (`--ch-bulk`)

```bash
# dry-run (default): print summary, nothing written
company-corpus register-financials --ch-bulk accounts_monthly_2024_01.zip

# bounded test run: first 100 filings only (dry-run)
company-corpus register-financials --ch-bulk accounts_monthly_2024_01.zip --limit 100

# write data/financials_register/<ch_number>.jsonl + coverage report
company-corpus register-financials --ch-bulk accounts_monthly_2024_01.zip --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints
entity / period / unbalanced counts without touching disk. `--limit N` caps the number
of filings processed and is gated for `--ch-bulk`, `--ee-file`, `--ee-year`, or
`--sk-id` (no equivalent for the NO `--orgnrs` / `--leis` path).

Arelle must be installed for `--ch-bulk` to function. If Arelle is absent the command
raises a clear `ImportError` pointing to `pip install '.[eu-financials]'`. The first
filing in a batch triggers a ~14 s FRC taxonomy download; subsequent files are ~0.7 s
each (taxonomy cached in the shared Arelle controller).

## Honest caveats — UK

### ~25% of accounts are paper or PDF

Companies House estimates roughly a quarter of annual accounts are filed as scanned PDFs
or paper with no machine-readable XBRL content. These appear in the bulk ZIP as `.html`
wrappers with no inline XBRL tags. The engine records them as `status="no-financials"`
and **never attempts OCR extraction**. Only structured iXBRL filings are processed.

### One period per entity per bulk file

The monthly bulk product contains each company's most recently filed accounts as of the
cut date — one filing, one period. Multi-year history requires iterating the monthly
archive. Historic backfill is deferred.

### LLP, charity, and CIC frameworks not fully handled

Limited Liability Partnerships (LLPs), charities (FRS 102 charities), and Community
Interest Companies (CICs) file under distinct FRC sub-frameworks with different local
names. The current mapping (`concepts_uk.py`) targets the standard FRS 105 / FRS 102 /
IFRS company taxonomy. Filings under LLP/charity/CIC frameworks are processed but
typically yield fewer tagged concepts.

### Annual accounts only

UK statutory accounts are annual. `frequency` is always `"annual"`. There are no
quarterly or semi-annual periods.

### No commercial P&L fallback — ever

The corpus is primary open data only. Revenue and income for balance-sheet-only filers
are **never supplemented from commercial databases, press releases, or any non-public
source**. If an iXBRL filing carries no tagged revenue, there is no revenue row. This is
a deliberate design constraint, not a coverage gap to be filled.

---

## Source — Belgium BNB CBSO annual accounts (dimensional XBRL)

Belgium's **Banque Nationale de Belgique Central Balance Sheet Office (BNB CBSO)**
collects mandatory annual statutory accounts from virtually all Belgian legal entities
and makes them available as **dimensional XBRL** instance documents. The XBRL model is
EBA/DPM-style: every monetary fact is qualified by its context's dimension members
(`bas`, `part`, `prd`, `ntr`, `rst`, `typ`, …) rather than carrying the meaning in the
element name. The CBSO uses a **version-stable `dict` member namespace**, so the
member-to-meaning mapping does not change across taxonomy releases.

The parser (`registers/bnb_xbrl.py`) uses **stdlib `xml.etree.ElementTree` only — no
Arelle, no taxonomy bundle**. Because meaning lives in the dimension members (not the
element names), the parser needs only to read context dimension members and numeric
values; it does not need the taxonomy at all. This makes it fast, dependency-free, and
resilient to taxonomy version changes.

### Two acquisition paths

| Path | Key required | Flag |
|---|---|---|
| **CBSO Authentic Data API** (targeted, per KBO) | Free subscription key | `--be-numbers` |
| **Local file** (downloaded `.xbrl` or deposit `.zip`) | None | `--be-file` |

**Authentic Data API:** The CBSO provides a free REST API at `https://ws.cbso.nbb.be`.
Registration is self-service at `https://developer.cbso.nbb.be`; the subscription key
is passed as `NBB-CBSO-Subscription-Key` header. The engine calls
`/authentic/legalEntity/{kbo}/references` to list available deposits, picks the most
recent by `DepositDate`, then fetches the accounting data. Live/scale validation —
rate limits, pagination, and quota behaviour for entities with large deposit histories —
is a **maintainer step** and is intentionally out of scope; the parser and pack are
validated against the public example filings.

**Keyless local-file path (`--be-file`):** A BNB deposit is either a bare `-data.xbrl`
file or a three-member deposit `.zip` (`*-contact.xbrl`, `*-data.xbrl`, `*-vendor.xbrl`).
Both are handled: the engine extracts the `*-data.xbrl` member automatically when it
receives a `.zip`. The KBO number is derived from the last underscore-delimited token
of the filename stem (e.g. `m02_full_0648822310.xbrl` → `"0648822310"`).

### Why BE is the richest register — borrowings-based leverage

BE is the **richest register in this corpus**. Unlike Norway (Brreg) and UK (Companies
House), which expose only broad liabilities aggregates, the BNB CBSO instance tags **real
financial borrowings** under the `bas:m51` rubric — bonds, bank loans, leases —
disaggregated by maturity (`rst`: long-term `m1` / short-term `m2`) and instrument type
(`typ`). The engine sums the validated `m51` tranches to compute `long_term_debt` and
`short_term_debt`, so:

- **`total_debt`** = financial borrowings (not total liabilities)
- **`debt_to_equity`** and **`debt_to_assets`** are **borrowings-based gearing**, not
  liabilities-based approximations

This is a qualitatively different and more informative metric than the liabilities-based
approximation produced for NO and UK. The `source` field (`"bnb"` vs `"brreg"` vs
`"companies_house"`) distinguishes the regimes in every output row. **Never compare
`debt_to_equity` from a BE row to a NO or UK row without first confirming that both
rest on the same debt basis.** The `source` field is the guard.

Additional richness: `revenue`, `dep_amort` (depreciation + amortisation), and
`net_income` + `income_tax` are all mapped — BE carries the full set of building blocks
for EBITDA derivation once `operating_profit` (m44) is unblocked (see Caveats). Depth
reaches approximately 18 years (2007 onwards). For comparison: Brreg typically provides
5–10 years and the UK bulk product provides one period per entity per monthly file.

## Schema — `data/financials_register/<kbo>.jsonl`

Output follows the same per-period row model as the NO and UK registers and the SEC/EU
pillars (see [`FINANCIALS.md`](FINANCIALS.md) for the full curated-concepts definition):

| Column | Value |
|---|---|
| `entity_id` | KBO enterprise number (10-digit zero-padded string) |
| `lei` | GLEIF LEI if resolved; `null` for the keyless `--be-file` path |
| `country` | `"BE"` |
| `source` | `"bnb"` |
| `basis` | `"company"` (statutory individual accounts; consolidated detection deferred) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` (CBSO holds only annual statutory accounts) |
| `currency` | Reporting currency from the XBRL unit (virtually always `"EUR"`) |
| `period_end` | ISO-8601 date (the maximum `endDate`/`instant` across all XBRL contexts) |
| `publication_date` | `null` (not extracted from the deposit) |

The concept pack mapped from the `m`-member dimensional space covers:

| Curated key | XBRL rubric | Notes |
|---|---|---|
| `assets` | `m25/m1` | Total balance-sheet assets (= total passif anchor) |
| `non_current_assets` | `m2/m1` | Fixed / non-current assets |
| `assets_current` | `m12/m1` | Current assets (BE-GAAP: includes receivables >1yr — see Caveats) |
| `cash` | `m23/m1` | Cash and cash equivalents |
| `inventory` | `m14/m1 sts=m2` | On-balance-sheet inventory |
| `receivables` | `m9/m1 rst=m2` | Short-term receivables |
| `equity` | `m37/m3 ntr=m4` | Shareholders' equity |
| `provisions` | `m47/m3` | Provisions (BE-GAAP: between equity and liabilities) |
| `liabilities` | `m50/m3` | Total liabilities |
| `liabilities_current` | `m50/m3 rst=m2` | Current liabilities |
| `revenue` | `m53/m4 ntr=m6` | Turnover |
| `net_income` | `m59/m4` | Period net result (breakdown-free; see note on pre-tax below) |
| `income_tax` | `m60/m4 spec=m17` | Income tax |
| `dep_amort` | `m2/m4 ntr=m6 mdp=m1` | Depreciation and amortisation |
| `long_term_debt` | derived from `m51` | Real financial borrowings, LT — see Confidence Gate |
| `short_term_debt` | derived from `m51` | Real financial borrowings, ST — see Confidence Gate |

Each `kind="reported"` row carries the source dimension selector as its `tag`
(e.g. `"m51 (derived, x-checked)"`), so downstream consumers can inspect exactly which
XBRL rubric fed each value.

The derived block produced from this pack includes (for a full filer):

- **Aggregates:** `total_debt` (borrowings), `net_debt`, `net_cash`, `working_capital`,
  `invested_capital`
- **Profitability:** `net_margin`, `roe`, `roa`
- **Leverage / liquidity:** `debt_to_equity` (borrowings), `debt_to_assets` (borrowings),
  `current_ratio`, `quick_ratio`, `cash_ratio`
- **Efficiency:** `asset_turnover`, `dso`

EBITDA and EBITDA-dependent metrics (`ebitda`, `ebitda_margin`, `net_debt_to_ebitda`)
are **not emitted** in the current release — `operating_profit` (m44) is suppressed
pending a second real validated example. When it is unblocked, the `dep_amort` mapping
already in place means EBITDA will be automatically computable without further work.

A coverage report is written to `data/reports/register_coverage_bnb.jsonl` for every
entity processed: `status="ok"` with a period count, `"no-financials"` (no usable facts
after gating), `"unbalanced"` (primary gate rejected the filing), or `"source-error"` (dead source or
parse failure). When items are suppressed by the confidence gate or the always-suppress
list, a `suppressed` list records each key and the reason.

## The confidence gate — no false data

**Governing principle:** a number known to be wrong must never be emitted; a missing
number is strictly better than a wrong one. The BNB instance is dimensional — meaning
lives in the dimension members, not the element names — so the central risk is **picking
a disaggregated fact instead of the total**. For example, `m59/m4 spec=m16` is the
pre-tax result while the **breakdown-free** `m59/m4` (no `spec` member) is the true
net result. The engine guards against this systematically.

### Canonical-member selection

For every curated key, the engine selects the **unique** fact whose `dims` equals
exactly `{bas, part, prd:m1} ∪ required-members` and carries **no other dimension**.
A disaggregated sub-breakdown (which carries an extra member like `spec=m16` or
`rst=m1`) can never match this exact set, so it can never masquerade as the total.
If 0 or more than 1 such fact exists — the total is ambiguous — the key is suppressed
and the reason recorded. The engine never defaults a missing value to zero.

### Primary gate: `m25/m1 == m25/m3`

Total assets (`m25/m1`) and total passif (`m25/m3`) are independently tagged in the
instance document. Under BE-GAAP, passif = equity + provisions + liabilities (note:
provisions sit between equity and liabilities, unlike the IFRS balance sheet where they
sit within liabilities — do not confuse the structures). If total assets and total passif
disagree beyond `max(2 EUR, 0.5% of the larger figure)`, the **entire filing is
rejected**: `status="unbalanced"`, no values emitted. A balance sheet that does not close
is untrustworthy by construction.

### Financial-debt cross-check — the borrowings guarantee

The `m51` rubric tags financial borrowings disaggregated by maturity (`rst`) and
instrument type (`typ`). The engine:

1. Collects all balance-sheet `m51` tranches (`bas=m51, ntr=m3, part=m3, prd=m1`),
   excluding the subordinated `sts` cross-cut that would double-count across tranches.
2. Checks that every remaining fact has **exactly** the expected dimension set
   `{bas, ntr, part, prd, rst, typ}` and a maturity bucket in `{m1, m2}`. A deviating
   structure (a subtotal without `typ`, a further sub-breakdown, an unexpected bucket)
   means the total cannot be confirmed — suppress the entire debt block.
3. Sums LT (`rst=m1`) and ST (`rst=m2`) tranches separately.
4. **Independent cross-check:** the financial-nature slice of total liabilities
   (`m50[ntr=m3]`, breakdown-free `rst=m1 + rst=m2` on the passif) is a **different
   rubric** from `m51`, so it is a genuinely independent witness. The `m51` sum must
   reconcile with this witness within `max(2 EUR, 0.5%)`.
5. Only if the cross-check passes are `long_term_debt` and `short_term_debt` emitted —
   **atomically**: if either half fails, neither is emitted. When the block is
   suppressed, the engine falls back to liabilities-based leverage (from the `m50`
   pack members) rather than emit a possibly-wrong borrowings figure.

### `operating_profit` — always suppressed

`operating_profit` (m44) is in the always-suppress list because its label is ambiguous
on the one validated real filing available — a second real example is required before
this concept can be safely unblocked. The reason is recorded in every filing's
`suppressed` list. This has no knock-on effect on `net_income` (independently tagged as
`m59/m4` breakdown-free) or any other concept.

### Pre-tax result is not net income

The dimensional model makes this explicit: `m59/m4 spec=m16` is the pre-tax result
(a disaggregation); the breakdown-free `m59/m4` is the net result. Because the
canonical-member selector requires an exact dimension match, the pre-tax breakout
cannot match the `net_income` selector and is automatically excluded. There is no risk
of the pre-tax figure being misreported as net income.

## The `basis` field — BE

All BE rows carry `basis="company"`. The BNB CBSO instance document embeds a
consolidation-model indicator (`m120`) that distinguishes individual accounts from
consolidated group accounts, but consolidated-model detection is deferred to a follow-up
PR. Until then, both individual and consolidated filings are emitted with
`basis="company"`.

Note that `basis="company"` rows from the BNB CBSO register and
`basis="consolidated"` rows from the EU ESEF pillar (`data/financials_eu/`) are already
separated by output directory. They must **not** be merged without an explicit accounting
and scope reconciliation: BE-GAAP differs materially from IFRS on lease accounting,
pension, deferred tax, and the treatment of provisions; and the obligor universe covered
by the register (all Belgian legal entities above the filing threshold) overlaps only
partly with the ESEF pillar (listed EU groups that issued regulated debt or equity).

## Identity — KBO and LEI

**KBO enterprise number (`entity_id`):** The KBO (Kruispuntbank van Ondernemingen)
enterprise number is the canonical Belgian legal-entity identifier — a 10-digit
zero-padded string (e.g. `"0648822310"`). It is always normalised to 10 digits
(stripping non-digits, left-zero-padding).

- **`--be-file` path:** the KBO is derived from the filename (last
  underscore-delimited stem token), so the file must be named consistently with the
  CBSO deposit convention.
- **`--be-numbers` path:** KBO numbers are passed directly on the command line and
  normalised to 10 digits.

**LEI resolution (library level):** When a spec carries a GLEIF LEI, the engine calls
`GET https://api.gleif.org/api/v1/lei-records/{lei}` and extracts
`entity.registeredAs`. This is accepted as the KBO **only when**
`entity.legalAddress.country == "BE"` — there is no guess, no fuzzy match. A LEI
from a non-Belgian jurisdiction returns `status="unresolved"` in the coverage report.
LEI resolution is available at the library level (`build_be_financials`); the CLI
`--be-numbers` flag takes KBO numbers directly and does not call GLEIF. In both modes,
the resolved `lei` (if available) is carried through to every output row.

## CLI usage — BE

```bash
# keyless path: parse one or more local .xbrl or deposit .zip files (dry-run)
company-corpus register-financials --be-file m02_full_0648822310.xbrl

# keyless path: multiple files, persist to disk
company-corpus register-financials \
  --be-file deposit_0200068636.zip deposit_0648822310.zip --write

# API path: fetch via CBSO Authentic Data API (requires free subscription key)
export BNB_CBSO_KEY=<your-key>
company-corpus register-financials --be-numbers 0648822310 0200068636

# API path: persist
company-corpus register-financials --be-numbers 0648822310 --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints
the entity / period / unbalanced counts without touching disk.

The `--be-file` and `--be-numbers` flags are mutually exclusive with each other and
with `--orgnrs`, `--leis`, and `--ch-bulk`.

**CBSO API key:** Register at `https://developer.cbso.nbb.be` (self-service, free). Set
the key in the `BNB_CBSO_KEY` environment variable before calling `--be-numbers`. The
key is passed as the `NBB-CBSO-Subscription-Key` header; the engine also sends a unique
`X-Request-Id` per request (CBSO requirement).

## Honest caveats — BE

### Leverage is borrowings-based (but compare with source-awareness)

The BE register is unique in this corpus in providing genuine financial-borrowings data
via `m51`. The derived `total_debt`, `debt_to_equity`, `debt_to_assets`, and `net_debt`
all rest on this borrowings basis — more precise than the total-liabilities basis used
for NO and UK. However:

- The `m50`-based fallback (liabilities-based) is used when the `m51` cross-check
  fails or the `m51` tranche structure deviates. The `source` and `tag` fields let
  downstream consumers identify which basis applies to a given row.
- **Never compare leverage metrics across registers without checking `source`:** a
  `debt_to_equity` from `source="bnb"` (borrowings) is not directly comparable to one
  from `source="brreg"` or `source="companies_house"` (liabilities-based).

### BE-GAAP statutory — separate from ESEF

BNB CBSO filings follow **BE-GAAP** (Belgian Generally Accepted Accounting Principles),
the statutory accounting standard for non-listed Belgian legal entities. Belgian listed
groups also file **ESEF consolidated accounts** under IFRS — those are ingested by the
EU ESEF pillar (`data/financials_eu/`) and must **never be merged** with BNB CBSO rows:
the GAAP regime, the consolidation scope, and the obligor population differ.

### BE-GAAP current-assets perimeter

Under BE-GAAP, **all trade receivables — including those due beyond one year — are
classified in current assets** (the `m12/m1` rubric). This means `assets_current`
includes long-dated receivables that IFRS would classify as non-current. Users comparing
BE current-asset figures to IFRS-based current assets for the same entity should be
aware of this classification difference; it affects `working_capital`, `current_ratio`,
`quick_ratio`, and any metric built on current assets.

### `operating_profit` suppressed

`operating_profit` (m44) is always suppressed in the current release — its label is
ambiguous on the one validated real filing and a second real example is required before
it can be safely unblocked. Consequently, `operating_margin`, `ebitda`, `ebitda_margin`,
`nopat`, `roic`, `interest_coverage`, and `net_debt_to_ebitda` are not produced for BE
rows. The `dep_amort` concept is mapped and ready; EBITDA will be automatically
derivable once `operating_profit` is validated.

### Consolidated-model detection deferred

All rows carry `basis="company"`. The CBSO instance embeds a consolidation indicator
(`m120`) that can distinguish individual from consolidated accounts, but reading it is
deferred to a follow-up PR. Until then, a consolidated filing processed via the BE
path will be tagged `basis="company"` — an inaccurate label that may mislead downstream
queries filtering by `basis`.

### Annual accounts only

BNB CBSO holds statutory annual accounts. There are no quarterly or semi-annual periods.
`frequency` is always `"annual"`.

### Live/scale validation requires the free CBSO key

The `--be-numbers` path requires the CBSO Authentic Data API subscription key, which is
free but requires self-service registration. The parser and concept pack are validated
against the public example filings published by the BNB; rate-limit behaviour,
pagination for entities with large deposit histories, and key-quota handling are
maintainer steps, not exercised in the unit tests.

---

## Source — Finland PRH avoindata XBRL open API

Finland's **Patent and Registration Office (PRH)** publishes statutory annual accounts
as **dimensional XBRL** instance documents on its **avoindata open-data platform**. The
API is fully public:

```
GET https://avoindata.prh.fi/opendata-xbrl-api/v3/financial?businessId=…&financialDate=…
```

**No API key, no registration, no authentication required** — every endpoint is
keyless. Two supporting endpoints cover entity history and population traversal:

| Endpoint | Purpose |
|---|---|
| `GET /v3/financials?businessId=…` | List available periods for one entity — returns `{"totalResults": N, "financials": [{"businessId": …, "financialDate": "YYYY-MM-DD"}, …]}` |
| `GET /v3/all_financials?financialDate=…` | Iterate all companies that filed for a given date (paginated, 100/page) |

The XBRL model is dimensional: every monetary fact sits in an element of the
`fi_met` / `fi_md103` namespace whose `contextRef` attribute links to a context
carrying the metric code as a `fi_dim:MCY` member (e.g. `fi_MC:x673` → revenue).
Comparative-period facts are distinguished by a `fi_dim:REF` member in the scenario;
the parser (`registers/fi_prh_xbrl.py`) keeps **current-period facts only**.

The parser uses **stdlib `xml.etree.ElementTree` only — no Arelle, no taxonomy
bundle**. Because meaning lives in the MCY dimension members (not the element names),
the parser needs only to read the MCY integer and the numeric value. This makes it
fast, dependency-free, and resilient to future taxonomy version changes.

Accounts filed at the PRH follow **Finnish GAAP (FAS)** — the statutory accounting
standard for Finnish legal entities filing individual (non-consolidated) accounts. FAS
differs materially from IFRS in the treatment of appropriations, tax, and provisions
(see the confidence gate below). The PRH register covers the **SME and private
universe**; large listed Finnish groups file consolidated ESEF accounts separately
(ingested by the EU ESEF pillar — never merged here).

## Schema — `data/financials_register/<ytunnus>.jsonl`

Output follows the same per-period row model as the NO/UK/BE registers and the
SEC/EU pillars (see [`FINANCIALS.md`](FINANCIALS.md) for the full curated-concepts
definition):

| Column | Value |
|---|---|
| `entity_id` | Y-tunnus (string; `NNNNNNN-N` format preserved, e.g. `"2919415-2"`) |
| `lei` | GLEIF LEI if resolved; `null` for the keyless `--fi-file` path |
| `country` | `"FI"` |
| `source` | `"prh"` |
| `basis` | `"company"` (FAS individual statutory accounts) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` (PRH holds only annual statutory accounts) |
| `currency` | Reporting currency from the XBRL unit (virtually always `"EUR"`) |
| `period_end` | ISO-8601 date string (instant of the current-period context) |
| `publication_date` | `null` (not exposed by the PRH API) |

The concept pack mapped from the `fi_MC` dimensional space covers:

| Curated key | fi_MC code | Notes |
|---|---|---|
| `revenue` | x673 | Turnover |
| `operating_income` | x689 | Operating result |
| `net_income` | x740 | **Final result after appropriations — never x738** (see gate) |
| `interest_expense` | abs(x4046) | Stored negative in the filing; taken as absolute value |
| `assets` | x360 | Total balance-sheet assets |
| `equity` | x435 | Shareholders' equity |
| `liabilities` | x513 | Total liabilities (confirmed total; maturity split suppressed — see gate) |
| `personnel_costs` | x1869 | Staff costs |
| `non_current_assets` | x376 | Fixed / non-current assets (may be negative; see gate) |
| `assets_current` | x424 | Current assets |

Each `kind="reported"` row carries its `fi_MC` code as `tag` (e.g. `"fi_MC:x360"`).

The derived block produced from this pack includes (for a full filer):

- **Margins:** `operating_margin`, `net_margin`
- **Returns:** `roa`, `roe`
- **Coverage:** `interest_coverage`
- **Efficiency:** `asset_turnover`

FI produces **no** `total_debt`, `net_debt`, `net_cash`, `debt_to_equity`,
`debt_to_assets`, `working_capital`, `current_ratio`, `quick_ratio`, or `cash_ratio`
— see the confidence gate below for the precise reasons. There is no `ebitda` (no
D&A in the pack) and no `effective_tax_rate` (income tax suppressed).

A coverage report is written to `data/reports/register_coverage_prh.jsonl` for every
entity processed: `status="ok"` with a period count, `"no-financials"` (no usable
facts after gating), `"unbalanced"` (primary balance gate failed), `"unresolved"`
(identity lookup failed), or `"source-error"` (dead source or parse failure). Suppressed items and their
reasons are always recorded in the `suppressed` list.

## The confidence gate — no false data

**Governing principle (from `concepts_fi.py`):** a number we cannot confirm must
never be emitted; a missing number is strictly better than a wrong one. Two
Finnish-specific traps shape the gate:

### The net-income trap — x740, never x738

Under Finnish GAAP, the P&L routes tax-like charges through **appropriations**
(`x541`). The pre-appropriations result `x738` is therefore **not** net income; the
final bottom line is `x740` (result after appropriations): `x740 = x738 + x541`.
The engine maps `net_income` to `x740` exclusively and verifies a **two-leg waterfall**
before emitting it:

- **Leg 1** (when both `x12` net financial items and `x738` are present):
  `|x689 + x12 − x738| ≤ tol` — if this fails, the P&L is untrusted and
  `net_income` is suppressed immediately.
- **Leg 2** (when `x738` is present):
  `|x738 + (x541 or 0) − x740| ≤ tol` — if this fails, the appropriations step is
  inconsistent and `net_income` is suppressed.
- When `x738` is absent both legs are skipped and `x740` is emitted directly (many
  abbreviated filings do not tag the intermediate result).

If either leg fails, `net_income` is suppressed. The engine **never falls back to
`x738`** — a pre-appropriations value labeled as net income would be a false figure
for any entity carrying appropriations.

Tolerance: `max(2 EUR, 0.5% of the scale)` — calibrated to the three real
fixtures (full, abbreviated, housing-company).

### Primary gate: `x360 == x435 + (x513 or 0)`

Total assets must equal equity plus total liabilities within tolerance. Mismatch →
the entire filing is untrustworthy: `unbalanced=True`, no values emitted at all.
When `x360` or `x435` is absent the gate cannot run and the parser proceeds
(directly-tagged values still stand), mirroring the BE/UK siblings.

### Asset decomposition gate: `x376 + x424 == x360`

When both asset components are present, the engine verifies their sum equals total
assets. On mismatch, **both `non_current_assets` and `assets_current` are suppressed**
(the gate-verified total `x360` still stands). Importantly: **`x376` (non-current
assets) may be negative** — Finnish housing companies regularly carry negative fixed
assets due to accumulated depreciation exceeding gross cost. This is structurally
correct under FAS; the engine accepts negative `x376` without a positivity check.

### The leverage trap — maturity split suppressed, `liabilities` reported as-is

The PRH instance documents carry `x583` and `x816` as the two maturity buckets of
total liabilities (`x513`), and these buckets reconcile to `x513` to the cent. The
problem is that the **PRH instance carries no label linkbase** — the taxonomy that
names which bucket is long-term vs short-term is external and not shipped with the
filing. There is therefore **no way to confirm from the data which of `x583`/`x816`
is long-term and which is short-term**.

Emitting a guessed `long_term_debt` / `short_term_debt` split would be a false
maturity claim. Mapping the total into a single maturity bucket would be an equally
false "all one maturity" assertion. Per the no-false-data governing principle the
engine therefore **suppresses the maturity split entirely** and records the reason.

Consequences:

- FI emits `liabilities` (`x513`) as a directly-tagged reported value.
- **No** `long_term_debt` or `short_term_debt` are emitted.
- **No** `total_debt`, `net_debt`, `net_cash`, `debt_to_equity`, or `debt_to_assets`
  are produced (the engine's leverage engine requires the maturity split).
- An authoritative PRH codelist confirming the `x583`/`x816` label assignment would
  re-enable the split under the existing reconciliation gate without any schema
  changes — this is the natural fast-follow.

### Always suppressed

| Key | Reason |
|---|---|
| `income_tax` | FAS routes tax through appropriations (x541); no confirmed income-tax line (x448 is not it) |
| `cash` | x438 cash semantics are ambiguous under FAS |
| `financial_debt` | No reliable bank-borrowings vs trade-payables split — suppressed |
| `provisions` | No confirmed provisions code on FAS filings |

## The `basis` field — FI

All FI rows carry `basis="company"`. PRH statutory accounts are individual legal-entity
accounts under FAS. The register does not expose consolidated group accounts; Finnish
groups file consolidated ESEF accounts separately.

Note that `basis="company"` rows from the PRH register and `basis="consolidated"` rows
from the EU ESEF pillar (`data/financials_eu/`) are already separated by output
directory. They must **never** be merged: FAS differs materially from IFRS (treatment
of appropriations, depreciation, pension, and deferred tax); the obligor population
(Finnish private companies) overlaps only partly with the ESEF pillar (Finnish listed
groups that issued regulated securities).

## Identity — Y-tunnus and LEI

**Y-tunnus (`entity_id`):** The Finnish business identifier — format `NNNNNNN-N`
(7 digits, hyphen, 1 check digit, e.g. `"2919415-2"`). Whitespace is stripped; the
hyphen and check digit are preserved verbatim. No leading-zero normalisation is
applied (the check digit is structurally distinct from the 7-digit body).

Two input modes:

- **`--fi-file` path:** the Y-tunnus is extracted from the filename using the
  `NNNNNNN-N` pattern (e.g. `fi_2919415-2_full_2024.xml` → `"2919415-2"`).
- **`--fi-businessid` path:** Y-tunnus values are passed directly on the command line.

**LEI resolution:** When a spec carries a GLEIF LEI, the engine calls
`GET https://api.gleif.org/api/v1/lei-records/{lei}` and extracts
`entity.registeredAs`. This is accepted as the Y-tunnus **only when**
`entity.legalAddress.country == "FI"` — the same country guard as the NO/BE
registers. A LEI from a non-Finnish jurisdiction returns `status="unresolved"` in
the coverage report. In both modes, the resolved `lei` (if available) is carried
through to every output row.

## CLI usage — FI

```bash
# keyless local path: parse one or more PRH XBRL .xml files (dry-run)
company-corpus register-financials --fi-file fi_2919415-2_full_2024.xml

# keyless local path: multiple files, persist to disk
company-corpus register-financials \
  --fi-file fi_2919415-2_full_2024.xml fi_0100379-9_abbrev_2023.xml --write

# keyless API path: fetch latest period via PRH open API (no key required)
company-corpus register-financials --fi-businessid 2919415-2

# keyless API path: multiple entities, persist
company-corpus register-financials --fi-businessid 2919415-2 0100379-9 --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints
the entity / period / unbalanced counts without touching disk. The `--fi-file` and
`--fi-businessid` flags are mutually exclusive with each other and with `--orgnrs`,
`--leis`, `--ch-bulk`, `--be-file`, and `--be-numbers`.

**API path behaviour:** for each Y-tunnus, the engine calls `GET /v3/financials` which
returns a `{"totalResults": N, "financials": [{"businessId": …, "financialDate": "YYYY-MM-DD"}, …]}`
envelope. The engine extracts the `financialDate` strings, picks the latest, then fetches
that period's XBRL via `GET /v3/financial`. A missing or erroring entity is recorded as
`"no-financials"` or `"source-error"` in the coverage report and never silently dropped.

## Honest caveats — FI

### Finnish GAAP (FAS), not IFRS — never merge with ESEF

PRH statutory accounts follow **Finnish GAAP (FAS)** — individual legal-entity
accounts for the private and SME universe. Finnish listed groups also file
**ESEF consolidated accounts** under IFRS; those are ingested by the EU ESEF pillar
(`data/financials_eu/`) and must **never be merged** with PRH rows. The GAAP regime,
consolidation scope, and entity population are all different.

### No leverage ratios (maturity-split fast-follow)

FI produces `liabilities` but **no `total_debt`, `debt_to_equity`, or
`debt_to_assets`** (see the confidence gate above). This is not a permanent
constraint: an authoritative PRH codelist confirming the `x583`/`x816` long/short
assignment would re-enable the maturity split under the existing reconciliation gate.
Until that codelist is confirmed, cross-register leverage comparisons should note that
FI rows carry no borrowings-based or liabilities-split gearing metric.

### Digital XBRL coverage is growing (~5% of registered companies)

The PRH avoindata platform holds XBRL filings from 2019 onwards. At present,
roughly 5% of Finnish registered companies have filed at least one digital XBRL
return — the remainder file PDF accounts. Coverage is growing year-on-year as larger
companies are brought into scope, but the universe today is a subset, not a census,
of Finnish companies.

### SME and private universe — large listed companies use ESEF

Finnish listed companies with securities admitted to trading on a regulated market are
required to file ESEF iXBRL consolidated accounts under IFRS. Their consolidated
figures appear in `data/financials_eu/`. The PRH avoindata register captures their
**individual (statutory entity) accounts** under FAS — a distinct and narrower filing
that is the obligor entity's own accounts, not the economic group.

### Annual accounts only

PRH statutory accounts are annual. `frequency` is always `"annual"`. There are no
quarterly or semi-annual periods in the avoindata XBRL API.

### No commercial fallback — ever

If the PRH API returns no XBRL filing for an entity, the entity is recorded as
`"no-financials"`. Revenue, income, and balance-sheet figures are **never
supplemented from commercial databases or any non-public source**. Open data only.

See also: [NO (Brreg)](#source--brreg-regnskapsregisteret-norway),
[UK (Companies House)](#source--uk-companies-house-statutory-accounts-ixbrl),
[BE (BNB CBSO)](#source--belgium-bnb-cbso-annual-accounts-dimensional-xbrl),
[`FINANCIALS.md`](FINANCIALS.md), [`EU_FINANCIALS.md`](EU_FINANCIALS.md).

---

## Source — Luxembourg LBR/STATEC Centrale des bilans (eCDF)

Luxembourg's **Centrale des bilans** is operated jointly by the LBR (Luxembourg Business
Registers) and STATEC. Annual statutory accounts are filed in the **eCDF (Electronic
Common Data Format)** schema — a bespoke XML format designed for Luxembourg GAAP. It is
**not XBRL**. STATEC publishes quarterly bulk dumps of all filed accounts on
**data.public.lu** under a **CC-BY-SA licence**:

```
https://download.data.public.lu/resources/donnees-comptes-annuels/
<date>T000000/comptes-annuels-<YYYY>-Q<N>.xml
```

**No API key, account, or registration is required.** Each quarterly file is a single
eCDF XML document containing all declarers for that period. The parser
(`registers/lu_ecdf.py`) uses **stdlib `xml.etree.ElementTree` only — no Arelle, no
taxonomy bundle**. The eCDF encoding is **ISO-8859-15**; the parser detects the encoding
from the XML declaration before transcoding to UTF-8 for `ElementTree`.

The stable **eCDF numeric code** (`<Field ecdf="NNN">`) is the semantic anchor. Codes
are stable across taxonomy versions (with version-specific exceptions noted below);
meaning lives in the code, not the element name.

Acquisition is **bulk-scan only**: one quarterly file per run, no per-entity API.
History reaches approximately **14 years (2012 onwards)**, split across two taxonomy
versions.

### Why LU is rich — borrowings-based leverage

Like BE, LU eCDF tags **real financial borrowings** disaggregated by instrument and
maturity, so:

- **`total_debt`** = financial borrowings (bonds + bank), not total liabilities
- **`debt_to_equity`** and **`debt_to_assets`** are **borrowings-based gearing**, not
  liabilities-based approximations

This is qualitatively the same level as the BE register — and a materially more
informative metric than the liabilities-based approximation produced for NO and UK.
The `source` field (`"lbr"` vs `"brreg"` / `"companies_house"` / `"bnb"`) distinguishes
the debt-basis regime in every output row. **Never compare `debt_to_equity` from a LU
row to a NO or UK row without first confirming that both rest on the same debt basis.**
The `source` field is the guard.

Beyond leverage, the LU pack also maps: `revenue` (eCDF 701), `participation_income`
(eCDF 715, holding-company dividend and distribution income), `net_income`, `income_tax`,
and `interest_expense`. The ~14-year depth and SOPARFI breadth make this register
particularly useful for Luxembourg holdco credit analysis.

## Schema — `data/financials_register/<rcs>.jsonl`

Output follows the same per-period row model as the other registers and the SEC/EU
pillars (see [`FINANCIALS.md`](FINANCIALS.md) for the full curated-concepts definition):

| Column | Value |
|---|---|
| `entity_id` | RCS number (verbatim string, e.g. `"B60814"`) |
| `lei` | GLEIF LEI if resolved; `null` for the keyless `--lu-file` path |
| `country` | `"LU"` |
| `source` | `"lbr"` |
| `basis` | `"company"` (statutory individual accounts; consolidated detection deferred) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` |
| `currency` | Reporting currency (virtually always `"EUR"`) |
| `period_end` | ISO-8601 date string (from the eCDF `<EndDate>` element) |
| `publication_date` | `null` (not exposed by the bulk XML) |

The concept pack mapped from the eCDF codes (for a full `CA_BILAN` declaration):

| Curated key | eCDF code(s) | Notes |
|---|---|---|
| `assets` | 201 | Total balance-sheet assets (primary gate anchor) |
| `cash` | 197 | Cash and cash equivalents |
| `equity` | 301 | Shareholders' equity |
| `provisions` | 331 | Provisions |
| `liabilities` | 435 (2016+) / 339 (2012) | Total liabilities (version-driven) |
| `net_result_bs` | 321 | Period result on the balance sheet (gate (c) cross-check anchor) |
| `revenue` | 701 | Turnover (full `CA_BILAN` only; see Declaration Types) |
| `participation_income` | 715 | Income from equity participations (holdco dividend income) |
| `net_income` | 669 (2016+) / 639−735 (2012) | Final net result — **never eCDF 667** |
| `income_tax` | 635 (signed) | Tax charge (positive = expense; 2016+ sign convention) |
| `interest_expense` | 627 (abs) | Interest expense (absolute value applied in both versions) |
| `long_term_debt` | derived | Sum of LT borrowings tranches; emitted only when cross-checked |
| `short_term_debt` | derived | Sum of ST borrowings tranches; emitted only when cross-checked |

Each `kind="reported"` row carries the eCDF code selector as its `tag`
(e.g. `"ecdf:669"`, `"ecdf:443+449+359"`), so downstream consumers can inspect the
exact code used.

The derived block produced from this pack (for a full filer) includes:

- **Aggregates:** `total_debt` (borrowings), `net_debt`, `net_cash`, `working_capital`,
  `invested_capital`
- **Profitability:** `net_margin`, `roe`, `roa`, `interest_coverage`
- **Leverage / liquidity:** `debt_to_equity` (borrowings), `debt_to_assets` (borrowings),
  `current_ratio`, `quick_ratio`, `cash_ratio`
- **Efficiency:** `asset_turnover`

EBITDA and EBITDA-dependent metrics are not produced (no D&A concept in the eCDF
schema). There is no cash-flow statement in the eCDF schema, so `free_cash_flow`,
`cfo_to_debt`, `fcf_to_debt`, and `net_debt_to_ebitda` are not emitted.

A coverage report is written to `data/reports/register_coverage_lbr.jsonl` for every
entity processed: `status="ok"` with a period count, `"no-financials"` (no usable values
after gating), `"unbalanced"` (primary gate rejected the filing), or `"source-error"` (dead source or
parse failure). Suppressed items and their reasons are recorded in the `suppressed` list.

## The confidence gate — no false data

**Governing principle:** a number known to be wrong must never be emitted; a missing
number is strictly better than a wrong one. The LU eCDF model has three structural traps
that require precise handling, in addition to the standard primary balance-sheet gate.

### Two taxonomy versions — dispatch before any code lookup

eCDF declarations exist in two taxonomy versions that differ on the debt codes, the
total-liabilities code, and the net-income code:

| Feature | 2012 taxonomy | 2016+ taxonomy ("2022") |
|---|---|---|
| Total liabilities code | 339 | 435 |
| Bonds / debentures code | 341 | 437 |
| Net income code | 639 − 735 | 669 |
| P&L sign convention | All unsigned positive | Expenses stored negative |
| Version-exclusive codes | — | 669, 435, 437 |

**Detection:** if any version-exclusive 2016+ code (`669`, `435`, or `437`) is present
in the declaration's fields, the taxonomy is 2016+; otherwise 2012. Keying only off
`669` (a P&L code) would misread a 2016+ balance-sheet declaration filed without a P&L
as 2012, and therefore read the wrong debt and liabilities codes. All three
version-exclusive codes are checked together.

### Primary gate (a): `201 == 405`

Total assets (`ecdf 201`) and total passif (`ecdf 405`) are independently tagged. Under
LU-GAAP, passif = equity + provisions + liabilities — a BE-GAAP-like structure where
provisions sit between equity and liabilities. If they disagree beyond
`max(2 EUR, 0.5% of the larger figure)`, the **entire filing is rejected**:
`status="unbalanced"`, no values emitted. A balance sheet that does not close is
untrustworthy by construction.

### Structural gate (b): passif decomposition

The passif decomposes as equity (301) + provisions (331) + liabilities (435 or 339,
version-driven) + result carried forward / minority (403). The engine checks:

```
301 + 331 + (435|339) + 403 == 405
```

Absent lines read as 0 (genuine zeros in LU-GAAP — a missing component means the line
is zero, not unknown). On failure, every passif-derived value — `equity`, `provisions`,
`liabilities`, `net_result_bs`, and the entire debt block — is suppressed.

### The `667`-vs-`669` net-income trap (2016+ only)

eCDF 2016+ distinguishes two closely-numbered result lines:

- **eCDF 667**: result *after income tax but before other taxes* — a pre-final subtotal
- **eCDF 669**: the **FINAL** net result (= 667 + 637, where 637 is other taxes)

**Net income is always `669` — never `667`.** If a 2016+ declaration carries `667`
but not `669`, the engine suppresses `net_income` and records the reason precisely
(`"667 present but 669 absent — refusing to fall back to 667"`). There is no silent
fallback to the pre-other-taxes figure.

For 2012 declarations the final result is `639` (profit) − `735` (loss); either may be
absent (reading as 0).

### Signed P&L convention (2016+ only)

The 2016+ P&L stores expenses as negative values. The engine normalises before emission:

- `interest_expense` = `abs(ecdf 627)` — always positive, both versions
- `income_tax` = `−ecdf 635` — positive = expense, negative = benefit (2016+ only;
  2012 is unsigned and taken as-is)

### BS/P&L cross-check gate (c)

When a balance-sheet and a P&L declaration are merged for the same period, the engine
cross-checks:

```
321 (BS net result) == net_income (P&L final result)
```

A mismatch beyond tolerance signals likely mismatched declarations (different periods or
filing versions). `net_income` is suppressed with the mismatch reason recorded; the
actif/passif and other directly-tagged values still stand.

### Financial-debt cross-check — the borrowings guarantee

The LT + ST maturity tranches are cross-checked against the independently-recorded
bonds + bank borrowings aggregate (a different set of eCDF codes):

```
sum(ST tranches) + sum(LT tranches) == bonds (437|341) + bank (355)
```

Because the aggregate and the maturity split are separate rubrics in the eCDF document,
this is a **genuinely independent cross-check**. The split is emitted **atomically**
only when it passes: if the reconciliation fails, **neither** `long_term_debt` nor
`short_term_debt` is emitted, and the engine falls back to the liabilities-based
`liabilities` pack member rather than emit a possibly-wrong borrowings figure.

## Declaration types and coverage

The LBR accepts six declaration types: three balance-sheet types (full / abridged /
SOPARFI), each optionally paired with a matching P&L declaration. The balance-sheet
type determines what can be derived:

| BS declaration type | Revenue | Debt breakdown |
|---|---|---|
| `CA_BILAN` (full accounts) | Yes (ecdf 701) | Yes (when cross-check passes) |
| `CA_BILANABR` (abridged accounts) | No (omitted by filer) | No (aggregate liabilities only) |
| `CA_BILANSOPARFI` (SOPARFI holdco) | No (participation income instead) | No (aggregate liabilities only) |

For abridged and SOPARFI types, `revenue` and the `long_term_debt`/`short_term_debt`
split are always suppressed — the latter because those declaration types report only
aggregate liabilities, not the borrowings isolation the cross-check requires. The
aggregate `liabilities` field still provides a liabilities-based leverage input.

## The `basis` field — LU

All LU rows carry `basis="company"`. eCDF declarations are statutory individual
accounts for the Luxembourg legal entity. Consolidated-account detection is deferred
to a follow-up PR.

Note that `basis="company"` rows from the LBR register and `basis="consolidated"` rows
from the EU ESEF pillar (`data/financials_eu/`) are already separated by output
directory. They must **not** be merged without an explicit accounting and scope
reconciliation: LU-GAAP differs from IFRS on multiple dimensions, and the obligor
populations overlap only partly.

## Identity — RCS and LEI

**RCS number (`entity_id`):** The Luxembourg *Registre de Commerce et des Sociétés*
(RCS) number is the canonical entity identifier, in the format `B<digits>` (e.g.
`"B60814"`). It is read verbatim from the `<RcsNumber>` element in the eCDF document;
no normalisation or digit-stripping is applied.

**LEI resolution (library level):** When a spec carries a GLEIF LEI, the engine calls
`GET https://api.gleif.org/api/v1/lei-records/{lei}` and extracts
`entity.registeredAs`. This is accepted as the RCS number **only when**
`entity.legalAddress.country == "LU"` — there is no guess, no fuzzy match. A LEI from
a non-Luxembourg jurisdiction returns `status="unresolved"` in the coverage report. For
the keyless `--lu-file` path, no GLEIF call is made and `lei` is `null` in every output
row.

## CLI usage — LU (`--lu-file`)

```bash
# dry-run (default): parse one quarterly bulk XML, print summary, nothing written
company-corpus register-financials --lu-file comptes-annuels-2023-Q4.xml

# filter to specific RCS numbers (dry-run)
company-corpus register-financials --lu-file comptes-annuels-2023-Q4.xml \
  --rcs B60814 B138357

# write data/financials_register/<rcs>.jsonl + coverage report
company-corpus register-financials --lu-file comptes-annuels-2023-Q4.xml --write

# multiple quarterly files in one pass (builds ~14yr history)
company-corpus register-financials \
  --lu-file comptes-annuels-2022-Q4.xml comptes-annuels-2023-Q4.xml --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints
entity / period / unbalanced counts without touching disk. `--rcs` accepts one or more
RCS strings and is available for `--lu-file` only; it filters within an already-
downloaded file. No API key, environment variable, or registration of any kind is
required.

Bulk files are published at
`https://download.data.public.lu/resources/donnees-comptes-annuels/` (CC-BY-SA). A
single quarterly file covers all declarers for that period; there is no per-entity API.

## Honest caveats — LU

### Universe — SOPARFI and commercial entities only; CSSF/CAA-regulated entities excluded

The LBR Centrale des bilans covers the **SOPARFI holdco and ordinary commercial**
universe — entities incorporated under Luxembourg commercial law that are required to
file statutory accounts at the LBR. This is credit-relevant for:

- Luxembourg-incorporated **holding companies (SOPARFIs)** that issue bonds or
  guarantee group debt
- Ordinary **commercial and industrial entities** operating in Luxembourg

**CSSF/CAA-supervised entities are excluded.** Banks, investment funds, insurance
companies, and authorised securitisation vehicles (SPVs under the 2004 Securitisation
Law) report to their respective supervisors (CSSF or CAA) rather than filing at the
LBR — they do **not** appear in the Centrale des bilans data. Do not use this register
to retrieve financials for Luxembourg-domiciled banks, funds, or regulated insurers.
Those entities are simply absent.

The practical consequence: the LU register has a **SOPARFI-heavy, debt-structure-
relevant** credit profile — particularly useful for holdco leverage analysis — but is
not the source for the regulated financial issuers that dominate IFRS-consolidated ESEF
filings.

### Leverage is borrowings-based (but compare with source-awareness)

The LU register provides genuine financial-borrowings data when the full `CA_BILAN`
type is present and the maturity cross-check passes. The derived `total_debt`,
`debt_to_equity`, `debt_to_assets`, and `net_debt` all rest on this borrowings basis.
However:

- When the cross-check fails or the declaration type is abridged/SOPARFI, the engine
  falls back to liabilities-based leverage from the `liabilities` pack member. The
  `source` field (`"lbr"`) and per-row `tag` identify which basis applies to a given row.
- **Never compare leverage metrics across registers without checking `source`:** a
  `debt_to_equity` from `source="lbr"` (borrowings) is not directly comparable to one
  from `source="brreg"` or `source="companies_house"` (liabilities-based).

### LU-GAAP statutory — separate from ESEF

LBR/STATEC filings follow **LU-GAAP** (Luxembourg Generally Accepted Accounting
Principles). Luxembourg listed groups also file **ESEF consolidated accounts** under
IFRS — those are ingested by the EU ESEF pillar (`data/financials_eu/`) and must
**never be merged** with LBR rows: the GAAP regime, consolidation scope, and obligor
population differ. The output directories (`data/financials_register/` vs
`data/financials_eu/`) enforce this separation.

### Bulk-scan only; no per-entity API

There is no per-entity API for the Centrale des bilans. Acquisition always requires
downloading a quarterly bulk XML (typically 100–400 MB per file). The `--rcs` flag
narrows processing within a downloaded file, but the download itself is always bulk.

### Annual accounts only

The Centrale des bilans holds statutory annual accounts. There are no quarterly or
semi-annual periods. `frequency` is always `"annual"`.

### Consolidated-model detection deferred

All rows carry `basis="company"`. eCDF declarations are statutory individual accounts;
consolidated-model detection is deferred to a follow-up PR.

### One period per entity per quarterly file

Each quarterly bulk file contains the most recently filed accounts as of the cut date —
one period per entity per file. Multi-year history (~14yr from 2012) requires iterating
multiple quarterly archives.

---

## Source — Denmark Erhvervsstyrelsen / Virk "Regnskaber" (XBRL)

Denmark's **Erhvervsstyrelsen** (Business Authority) requires annual statutory accounts
from all Danish legal entities above the filing threshold. They are collected and made
publicly available through the **Virk "Regnskaber"** (accounts) open-data portal. The
engine queries the Virk **Elasticsearch** endpoint:

```
POST http://distribution.virk.dk/offentliggoerelser/_search
```

**No API key, account, or registration is required** — the endpoint is fully public and
keyless, returning up to 50 hits sorted newest-first via a `term` query on `cvrNummer`.
Documents (bare XBRL and PDF) are fetched by individual HTTP GETs. The corpus holds
approximately **4 million XBRL filings** covering the private and listed Danish universe.

**Gzip caveat:** The Virk document server sends payloads as gzip-compressed bytes but
**omits the `Content-Encoding: gzip` response header**. The HTTP layer therefore does not
auto-decompress. The engine inspects the first two bytes of every downloaded document:
if they match the gzip magic (`\x1f\x8b`), the payload is decompressed explicitly before
parsing. Passing a compressed payload directly to the XML parser without this step would
raise a parse error.

### Two acquisition and parsing paths

The engine distinguishes two XBRL formats, routing by sniffing the first 8 KB of the
document for known namespace markers:

| Path | `source` | Format | Parser | Leverage basis |
|---|---|---|---|---|
| **A — listed ESEF/IFRS** | `"erst-ifrs"` | `ifrs-full` bare XBRL | stdlib (`parse_virk_esef_xml`) | **Borrowings-based** |
| **B — private DK-GAAP/FSA** | `"erst-fsa"` | FSA taxonomy bare XBRL | stdlib (`parse_fsa_facts`) | **Liabilities-based** |

#### Path A — listed ESEF/IFRS (`source="erst-ifrs"`)

Listed Danish issuers file a bare `ifrs-full` XBRL instance. The parser
(`registers/concepts_dk.py::parse_virk_esef_xml`) is **stdlib `xml.etree.ElementTree`
only — no Arelle, no taxonomy bundle**. It:

- Locates the `ifrs-full` namespace URI by substring (handles both the 2023-03-27 and
  2024-03-27 taxonomy vintages without relying on the `ifrs-full` prefix, which filing
  tools can remap).
- Indexes **no-dimension** contexts only: any `xbrli:context` whose `xbrli:scenario`
  carries child elements (equity-component splits, maturity buckets, …) is excluded,
  preventing disaggregated facts from leaking into the top-line totals.
- Produces the flat `{local_name: [datapoint]}` shape that feeds directly into
  **`summaries_from_flat(flat, concepts=IFRS_CONCEPTS)`** — 100% reuse of the existing
  EU IFRS engine with no new infrastructure.

Because the IFRS engine is reused without modification, Path A automatically gets
**borrowings-based leverage** via `ifrs-full:NoncurrentBorrowings` +
`ifrs-full:CurrentBorrowings`, the same formula applied by the EU ESEF pillar.

#### Path B — private DK-GAAP/FSA (`source="erst-fsa"`)

Private companies file bare XBRL under the **Erhvervsstyrelsen FSA taxonomy**
(`http://xbrl.dcca.dk/fsa`). The parser (`registers/dk_fsa_xbrl.py::parse_fsa_facts`)
is also **stdlib `xml.etree` only — no Arelle, no taxonomy bundle**. Namespaces are
matched by URI in Clark notation (`{uri}LocalName`) — never by prefix, since filing
tools may reassign prefix names.

**Context selection:** Private (class-B) DK filings carry no `ConsolidatedSoloDimension`
— their contexts have no scenario at all. Selection priority is: no-dimension contexts
(no `xbrli:scenario` child, or an empty one), then `cmn:ConsolidatedSoloDimension =
SoloMember`, then `ConsolidatedMember`. Mixed-basis facts are never emitted together.

**Current-period selection (no period-mixing):** A DK-GAAP filing carries the current
year and prior-year comparative figures side by side. The parser anchors on the **max
`xbrli:instant`** across the selected contexts as `period_end`; only facts from contexts
whose date equals `period_end` are emitted. Prior-year comparative facts are excluded
entirely — they can never leak into the current-period row, so the balance-sheet identity
holds for the current period alone even when the entity changed size year over year.

The concept pack mapped from FSA local names is:

| Curated key | FSA element | Notes |
|---|---|---|
| `assets` | `fsa:Assets` | Total balance-sheet assets (primary gate anchor) |
| `assets_current` | `fsa:CurrentAssets` | Current assets |
| `cash` | `fsa:CashAndCashEquivalents` | Cash and equivalents |
| `equity` | `fsa:Equity` | Shareholders' equity |
| `net_income` | `fsa:ProfitLoss` | Period net result |
| `gross_profit` | `fsa:GrossProfitLoss` | Bruttoresultat — **not revenue** (see §32 gate below) |
| `liabilities` | derived: `LiabilitiesAndEquity − Equity` | Captures provisions automatically |
| `provisions` | `fsa:Provisions` | Directly tagged; reported separately, never counted as debt |
| `revenue` | `fsa:Revenue` | Suppressed when absent (§32 — see confidence gate below) |
| `short_term_debt` | `fsa:ShorttermLiabilitiesOtherThanProvisions` | Emitted only when reconciling (see gate) |
| `long_term_debt` | `fsa:LongtermLiabilitiesOtherThanProvisions` | Emitted only when reconciling (see gate) |

`financial_debt` (borrowings-by-instrument) is always suppressed — class-B filings carry
no bank/bond/lease split.

## Schema — `data/financials_register/<cvr>.jsonl`

Output follows the same per-period row model as the other registers and the SEC/EU
pillars (see [`FINANCIALS.md`](FINANCIALS.md) for the full curated-concepts definition).
Both paths write to the same per-CVR file; the `source` column distinguishes them:

| Column | Value |
|---|---|
| `entity_id` | CVR number (8-digit string, e.g. `"30830725"`) |
| `lei` | GLEIF LEI if resolved; `null` for the keyless `--dk-file` FSA path |
| `country` | `"DK"` |
| `source` | `"erst-ifrs"` (Path A — listed ESEF) or `"erst-fsa"` (Path B — DK-GAAP FSA) |
| `basis` | `"company"` (statutory individual accounts) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` |
| `currency` | `"DKK"` (both paths; never EUR) |
| `period_end` | ISO-8601 date string |
| `publication_date` | `null` (not exposed by the Virk API) |

Each row is `kind="reported"` (directly-tagged or cleanly derived curated concepts, each
carrying its source tag) or `kind="derived"` (single-period metrics from the shared
`compute_derived` engine). No `derived_ttm` rows (annual accounts only).

**Path A derived block** (for a full ESEF filer): the IFRS engine's standard output —
`total_debt` (borrowings), `net_debt`, `net_cash`, `debt_to_equity` (borrowings-based),
`debt_to_assets` (borrowings-based), `operating_margin`, `net_margin`, `roe`, `roa`,
`current_ratio`, `interest_coverage`, and more — exactly as the EU ESEF pillar produces.

**Path B derived block** (for a full DK-GAAP filer, when the maturity split reconciles):
`total_debt` (= total liabilities, liabilities-based), `net_debt`, `net_cash`,
`working_capital`, `debt_to_equity` (liabilities-based), `debt_to_assets`
(liabilities-based), `net_margin`, `roe`, `roa`. There is no `ebitda` (no D&A concept in
the FSA pack) and no `free_cash_flow`, `cfo_to_debt`, or `fcf_to_debt` (no cash-flow
statement).

Coverage is written per source: `data/reports/register_coverage_erst-ifrs.jsonl` (Path A
— listed ESEF) and `data/reports/register_coverage_erst-fsa.jsonl` (Path B — DK-GAAP
FSA), for every entity processed: `status="ok"` with a period count, `"no-financials"`
(no usable facts after gating), `"unbalanced"` (primary gate rejected the filing), or
`"source-error"` (dead source or parse failure). Suppressed items and their reasons are always recorded in
the `suppressed` list.

## The confidence gate — no false data

**Governing principle:** a number we cannot confirm must never be emitted; a missing
number is strictly better than a wrong one. Two DK-GAAP traps specific to the FSA
taxonomy drive the Path B gate design.

### The §32 / GrossProfitLoss ≠ revenue trap (Path B)

Under ÅRL (the Danish Financial Statements Act) §32, class-B and micro-entity filers are
permitted to present only **Bruttoresultat** (gross result) as their top P&L line —
revenue minus cost of goods sold and external costs — and to **omit `fsa:Revenue`
entirely**. Bruttoresultat is tagged as `fsa:GrossProfitLoss`.

**`fsa:GrossProfitLoss` is not revenue.** Mapping it to `revenue` would silently
understate the top line — the difference equals the cost of goods sold and external
costs, which can be very large. The engine therefore:

- Emits `revenue` **only from `fsa:Revenue`** when that tag is present.
- When `fsa:Revenue` is absent (the §32 case), **suppresses `revenue` entirely** and
  records the reason.
- Emits `gross_profit` from `fsa:GrossProfitLoss` under its own correctly-labelled key.

`gross_profit` and `revenue` are thus never confused. A query on `revenue` for a §32
filer returns no rows — not a wrong value.

### Primary gate: `Assets == LiabilitiesAndEquity` (Path B)

`fsa:Assets` and `fsa:LiabilitiesAndEquity` are independently tagged. If they disagree
beyond `max(2 DKK, 0.5% of the larger figure)`, the **entire filing is rejected**:
`status="unbalanced"`, no values emitted. A balance sheet that does not close is
untrustworthy by construction.

When either anchor is absent the gate cannot run; the parser proceeds and emits whatever
directly-tagged values are present, mirroring the BE/FI/UK siblings.

### Total liabilities — derived, captures provisions automatically

`liabilities` is derived as `LiabilitiesAndEquity − Equity`. Under DK-GAAP, the passiv
comprises equity + provisions + liabilities proper — provisions sit between equity and
liabilities (similar to BE-GAAP). Deriving from the total captures provisions
automatically; no separate provisions gate is needed.

### Leverage: maturity split gated, atomic emission (Path B)

The FSA taxonomy labels the maturity buckets unambiguously:
`fsa:ShorttermLiabilitiesOtherThanProvisions` (short) and
`fsa:LongtermLiabilitiesOtherThanProvisions` (long). No guessing is required. The split
is emitted **only when**:

1. At least one bucket is tagged in the filing.
2. `short + long + provisions` reconciles to the derived total liabilities within
   tolerance — the split fully accounts for the passiv.

When the reconciliation holds, each tagged bucket is emitted with its confirmed taxonomy
label. A bucket the filing did not tag is left absent (never synthesised — a zero long-
term tranche is genuinely unknown without the tag). When the reconciliation fails — or
neither bucket is present — the **entire split is suppressed atomically** and the reason
is recorded; provisions are always kept separate and never counted as debt.

This is a **liabilities-based** approximation (total non-provision liabilities, not pure
financial borrowings), analogous to the NO and UK registers. Class-B filings do not tag
borrowings by instrument; class-C/D confirmation is a deferred fast-follow (see Caveats).

### Balance gate: `Assets == Equity + Liabilities` (Path A)

For ESEF/IFRS filings the engine verifies the IFRS balance-sheet identity after
`summaries_from_flat`: `Assets == Equity + Liabilities` within tolerance. On failure the
summary is retained with a warning rather than discarded (partial financials remain
useful downstream); callers may apply stricter filtering if needed.

## The `basis` field — DK

All DK rows carry `basis="company"`. Both paths ingest statutory individual accounts.
Consolidated-group detection is deferred to a follow-up PR.

The output directory (`data/financials_register/`) is separate from the EU ESEF pillar
(`data/financials_eu/`). Rows from the two pillars must **never** be merged:

- Path A (`source="erst-ifrs"`) rows contain IFRS figures from the `ifrs-full` taxonomy,
  but are filed at Erhvervsstyrelsen rather than through the ESEF mandatory chain — they
  may cover entities not in the ESEF corpus, and the filing provenance differs.
- Path B (`source="erst-fsa"`) rows follow DK-GAAP (ÅRL), which differs from IFRS in
  the treatment of provisions, appropriations, and depreciation.

## Identity — CVR and LEI

**CVR (`entity_id`):** The Danish CVR (Central Business Register) number — an 8-digit
string (e.g. `"30830725"`) — is the canonical legal-entity identifier.

- **FSA path (Path B):** CVR is extracted from the `xbrli:identifier` element whose
  `scheme` URI contains `dcca` (e.g. `http://www.dcca.dk/cvr`). Falls back to the first
  8-digit sequence in the filename stem when not found in the XML.
- **ESEF path (Path A) via file:** The `xbrli:identifier` uses an ISO 17442 LEI scheme
  (scheme URI contains `17442`); the extracted value is the LEI. CVR requires a GLEIF
  hop (see below) and is not embedded in the ESEF XML.
- **`--dk-cvr` API path:** CVR numbers are passed directly on the command line.

**LEI resolution:** When a spec carries a GLEIF LEI, the engine calls
`GET https://api.gleif.org/api/v1/lei-records/{lei}` and extracts
`entity.registeredAs`. This is accepted as the CVR **only when**
`entity.legalAddress.country == "DK"` — the same country guard as the NO/BE/FI
registers. A LEI from a non-Danish jurisdiction returns `status="unresolved"` in the
coverage report. The resolved `lei` (if available) is carried through to every output row.

## CLI usage — DK

```bash
# keyless local path: parse one or more Virk XBRL .xml files (dry-run, ESEF or FSA)
company-corpus register-financials --dk-file dk_30830725_fsa_2025.xml

# keyless local path: multiple files, persist to disk
company-corpus register-financials \
  --dk-file dk_30830725_fsa_2025.xml dk_42566551_fsa_2024.xml --write

# keyless API path: fetch latest annual report from Virk Regnskaber by CVR (dry-run)
company-corpus register-financials --dk-cvr 30830725

# keyless API path: multiple CVR numbers, persist
company-corpus register-financials --dk-cvr 30830725 42566551 --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints
entity / period / unbalanced counts without touching disk. The `--dk-file` and
`--dk-cvr` flags are mutually exclusive with each other and with `--orgnrs`, `--leis`,
`--ch-bulk`, `--be-file`, `--be-numbers`, `--fi-file`, `--fi-businessid`, and
`--lu-file`.

The `--dk-file` path is entirely local (no network). The `--dk-cvr` path issues a `POST`
to `distribution.virk.dk/offentliggoerelser/_search` and then one or more `GET` requests
per filing, with auto-gunzip applied to every document. No API key, environment variable,
or registration is required for either path.

## Honest caveats — DK

### Currency DKK — never compare to EUR registers

All DK rows carry `currency="DKK"`. Denmark has not adopted the euro. **Never compare
monetary values across DK rows and the EUR-currency registers (BE, LU, FI) without
applying an exchange-rate conversion.** The `currency` column in every row is the guard.

### Path B is liabilities-based — class-C/D borrowings fast-follow

DK-GAAP class-B filings do not tag borrowings by instrument (bank debt, bonds, leases).
The maturity split approximates `short_term_debt` / `long_term_debt` as total
non-provision liabilities — not pure financial borrowings. All derived gearing
(`debt_to_equity`, `debt_to_assets`, `net_debt`) for `source="erst-fsa"` rows is
therefore **liabilities-based**, not borrowings-based, analogous to the NO and UK
registers.

Whether class-C and class-D (large Danish companies) filings in the FSA taxonomy expose
borrowings-by-instrument tags is a **deferred confirmation**. If confirmed, the engine
can be extended under the existing maturity-split gate without schema changes.

### Path A is borrowings-based — never blend with Path B

Path A (`source="erst-ifrs"`) uses the IFRS `NoncurrentBorrowings` / `CurrentBorrowings`
concepts — pure financial borrowings, the same basis as the BE and LU registers. Path B
(`source="erst-fsa"`) is liabilities-based. **Never compare `debt_to_equity` from an
`erst-ifrs` row to one from an `erst-fsa` row without first confirming that both rest on
the same debt basis.** The `source` field is the guard.

### Revenue absent for §32 / class-B filers

Class-B and micro-entity filers under ÅRL §32 typically omit `fsa:Revenue` and present
only `fsa:GrossProfitLoss` (Bruttoresultat). For these filers `revenue` is suppressed
and recorded in the coverage report's `suppressed` list. `gross_profit` (Bruttoresultat)
is emitted under its own key. Do not screen DK-GAAP filers by revenue without first
filtering for filers that actually carry the `revenue` concept; use `gross_profit` as the
top-line metric for class-B filings.

### DK-GAAP statutory (Path B) — separate from ESEF

Path B rows follow **DK-GAAP (ÅRL)** — the statutory accounting standard for Danish
private legal entities. Danish listed groups also file **ESEF consolidated accounts**
under IFRS; those are ingested by the EU ESEF pillar (`data/financials_eu/`) and must
**never be merged** with `source="erst-fsa"` rows: the GAAP regime, consolidation scope,
and entity population all differ.

Path A rows (`source="erst-ifrs"`) come from the same `ifrs-full` taxonomy as the ESEF
pillar but are filed through Virk rather than the ESEF mandatory chain — they should
also be kept in `data/financials_register/` and reconciled explicitly before any merge
with `data/financials_eu/`.

### Annual accounts only

Danish statutory accounts are annual. `frequency` is always `"annual"`. There are no
quarterly or semi-annual periods in the Virk Regnskaber data.

### No commercial fallback — ever

If Virk returns no XBRL filing for a CVR, the entity is recorded as `"no-financials"`.
Revenue and balance-sheet figures are **never supplemented from commercial databases or
any non-public source**. Open data only.

---

## Source — Estonia Äriregister (RIK) avaandmed (bulk CSV)

Estonia's **Äriregister (RIK — Registrite ja Infosüsteemide Keskus)** publishes
annual statutory accounts as **two bulk CSV exports** on its open-data platform:

```
https://avaandmed.ariregister.rik.ee/et/avaandmete-allalaadimine
```

**No API key, no registration, no authentication required** — every download is
completely open. Both exports are published under a **CC-BY 4.0** licence.

| Export | File pattern | Content |
|---|---|---|
| **Elements** | `4.<year>_aruannete_elemendid_<snapshot>.zip` | One row per financial element per report; semicolon-delimited, columns `report_id;tabel;elemendi_label;elemendi_nimetus;vaartus` |
| **Metadata** | `1.aruannete_yldandmed_<snapshot>.zip` | One row per report: `report_id`, `registrikood`, `aruandeaasta`, `period_end` (DD.MM.YYYY), `kas konsolideeritud?` |

The two exports are joined on `report_id`. The critical feature of the elements
export is that `elemendi_nimetus` carries the **English et-gaap element name** already
extracted — `"Assets"`, `"Equity"`, `"CurrentLiabilities"`,
`"TotalAnnualPeriodProfitLoss"`, etc. The mapping is therefore a **pure stdlib
CSV-join** (`csv` + `zipfile`) with a direct key-to-element lookup — **no XBRL
parser, no Arelle**. This is the simplest acquisition path in the register pillar.

**Standalone-only:** any element whose `elemendi_nimetus` ends in `"Consolidated"` is
dropped at parse time. The plain names are always the standalone legal-entity figures
(`basis="company"`). Consolidated variants are excluded and never mixed with the
for-profit standalone pack.

File names include a rotating snapshot date in their stems. The `--ee-year` online
path requires explicit `--ee-elem-url` and `--ee-meta-url` arguments (the snapshot
date is not predictable from the year alone); obtain the current URLs from the RIK
download listing at the address above.

History covers **FY 2019–2025** (annual cuts). The database holds approximately
**321 000 registered companies** (~65% dormant or micro — see Caveats).

## Schema — `data/financials_register/<registrikood>.jsonl`

Output follows the same per-period row model as the other registers and the SEC/EU
pillars (see [`FINANCIALS.md`](FINANCIALS.md) for the full curated-concepts
definition):

| Column | Value |
|---|---|
| `entity_id` | Registrikood (8-digit zero-padded string, e.g. `"10003666"`) |
| `lei` | GLEIF LEI if resolved; `null` for the keyless `--ee-file` path |
| `country` | `"EE"` |
| `source` | `"rik"` |
| `basis` | `"company"` (standalone legal-entity accounts) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` (RIK holds only annual statutory accounts) |
| `currency` | `"EUR"` (Estonia adopted the euro in 2011; all accounts are in EUR) |
| `period_end` | ISO-8601 date string (`DD.MM.YYYY` from the bulk is parsed to `YYYY-MM-DD`) |
| `publication_date` | `null` (not exposed by the bulk export) |

The concept pack mapped directly from the et-gaap element names:

| Curated key | et-gaap element | Notes |
|---|---|---|
| `assets` | `Assets` | Total balance-sheet assets (primary gate anchor) |
| `assets_current` | `CurrentAssets` | Current assets |
| `non_current_assets` | `NonCurrentAssets` | Non-current assets |
| `cash` | `CashAndCashEquivalents` | Cash and equivalents |
| `equity` | `Equity` | Shareholders' equity (primary gate anchor) |
| `liabilities_current` | `CurrentLiabilities` | Current liabilities (working capital, liquidity) |
| `short_term_debt` | `CurrentLiabilities` | Liabilities-based leverage split — see Caveats |
| `long_term_debt` | `NonCurrentLiabilities` | Liabilities-based leverage split — see Caveats |
| `revenue` | `Revenue` | Turnover |
| `operating_income` | `TotalProfitLoss` | Operating result (*ärikasum*) — **not** net income; see gate |
| `pretax_income` | `TotalProfitLossBeforeTax` | Result before income tax |
| `net_income` | `TotalAnnualPeriodProfitLoss` | FINAL net result (after income tax) — see gate |
| `dep_amort` | `DepreciationAndImpairmentLossReversal` | Stored as `abs()` — source is a negative cost line |

Each `kind="reported"` row carries the et-gaap element name as its `tag`
(e.g. `"et-gaap:Assets"`), so downstream consumers can inspect the exact element used.

The derived block produced from this pack (for a full filer) includes:

- **Profitability:** `operating_margin`, `net_margin`, `ebitda`, `ebitda_margin`
  (`nopat` and `effective_tax_rate` are **not** produced — the EE pack carries no
  `income_tax` line)
- **Returns:** `roe`, `roa` (`roic` is **not** produced — requires `income_tax`)
- **Aggregates:** `total_debt`, `net_debt`, `net_cash`, `working_capital`,
  `invested_capital`
- **Leverage / liquidity:** `debt_to_equity`, `debt_to_assets`, `current_ratio`,
  `quick_ratio`, `cash_ratio`
- **Efficiency:** `asset_turnover`, `dso`

`interest_coverage` is **never available** — the RIK bulk contains no interest or
borrowings element (confirmed across the full dataset); it is always suppressed and
recorded in the coverage report. `net_debt_to_ebitda`, `free_cash_flow`, `cfo_to_debt`,
and `fcf_to_debt` are not emitted (no cash-flow statement in the bulk).

A coverage report is written to `data/reports/register_coverage_rik.jsonl` for every
entity processed: `status="ok"` with a period count, `"no-financials"` (NGO/non-profit
template detected — see gate), `"unbalanced"` (primary gate rejected the filing), or
`"source-error"` (dead source or parse failure). Suppressed items and their reasons are always recorded in
the `suppressed` list.

## The confidence gate — no false data

**Governing principle:** a number known to be wrong must never be emitted; a missing
number is strictly better than a wrong one. Two Estonian-specific traps shape the gate.

### The net-income trap — `TotalAnnualPeriodProfitLoss`, never `TotalProfitLoss`

The Estonian P&L waterfall has three clearly-named levels that are easy to confuse:

| et-gaap element | Estonian label | Meaning |
|---|---|---|
| `TotalProfitLoss` | *ärikasum* | **Operating** result — before financial items and tax |
| `TotalProfitLossBeforeTax` | *kasum enne tulumaksu* | Result before income tax |
| `TotalAnnualPeriodProfitLoss` | *aruandeaasta kasum* | **FINAL** net result (after income tax) |

**`net_income` is always `TotalAnnualPeriodProfitLoss` — never `TotalProfitLoss`
(operating) nor `TotalProfitLossBeforeTax` (pretax).** Mapping either earlier line as
net income would produce a materially wrong figure for any entity carrying financial
items or income tax. The three lines go to three distinct keys (`operating_income` /
`pretax_income` / `net_income`) and are never conflated.

Note: Estonia's corporate income tax system taxes dividends on distribution rather than
profits on accrual, so most Estonian companies carry no accrual income tax and
`TotalAnnualPeriodProfitLoss == TotalProfitLossBeforeTax`. However, a minority do
carry accrual tax (e.g. registrikood `10524187`: pretax 2 919 396 → net 2 637 345).
Mapping `TotalProfitLossBeforeTax` as `net_income` would silently overstate income for
those entities — unacceptable.

### Primary gate: `Assets == Equity + CurrentLiabilities + NonCurrentLiabilities`

All four balance-sheet components are **directly tagged** in the et-gaap element set —
no derivation is needed. The gate verifies:

```
|Assets − (Equity + CurrentLiabilities + NonCurrentLiabilities)| ≤ tol
```

where `tol = max(2 EUR, 0.005 · |Assets|)` (0.5% of assets, never tighter than 2 EUR
for micro-entity rounding). Absent liability buckets count as 0 (a zero-debt filer).
Mismatch → `unbalanced=True`, **no values emitted**: a wrong balance sheet is worse
than none. Validated to the cent on all 13 real fixtures used in development.

### NGO / non-profit template guard

The RIK bulk mixes **for-profit company accounts** (using `Assets` / `Equity`) and
**NGO / non-profit accounts** (using `LiabilitiesAndNetAssets` /
`NetSurplusDeficitForPeriod`). Mapping an NGO report into the company schema would
emit mislabelled figures. When either `Assets` or `Equity` is absent, the report is
treated as `"no-financials"` — the reason is recorded in the coverage report and no
values are emitted. This is a clean `no-financials` status, not `unbalanced`.

### Always suppressed

| Key | Reason |
|---|---|
| `interest_expense` | No interest element exists anywhere in the RIK bulk — suppressed (no false data) |
| `interest_coverage` | No interest or borrowings element in the bulk — coverage ratio not computable — suppressed (no false data) |

## The `basis` field — EE

All EE rows carry `basis="company"`. The RIK avaandmed bulk is standalone-only:
`*Consolidated` element variants are dropped at parse time, so only the legal entity's
own statutory accounts flow through. Consolidated-group accounts for Estonian entities
that are part of larger groups are not available from this source.

Note that `basis="company"` rows from the RIK register and `basis="consolidated"` rows
from the EU ESEF pillar (`data/financials_eu/`) are already separated by output
directory. They must **not** be merged without an explicit accounting and scope
reconciliation: Estonian GAAP (Estonian Accounting Standards / EAS) is based on IFRS
but differs in application; the obligor population (all Estonian legal entities above
the filing threshold) overlaps only partly with the ESEF pillar (Estonian listed groups
that issued regulated securities).

## Identity — registrikood and LEI

**Registrikood (`entity_id`):** The Estonian company registration code — an 8-digit
zero-padded numeric string (e.g. `"10003666"`). Non-digit characters are stripped and
the result is left-zero-padded to 8 digits. It is taken directly from the
`registrikood` column in the metadata CSV; no external lookup is needed.

**LEI resolution:** When a spec carries a GLEIF LEI, the engine calls
`GET https://api.gleif.org/api/v1/lei-records/{lei}` and extracts
`entity.registeredAs`. This is accepted as the registrikood **only when**
`entity.legalAddress.country == "EE"` — the same country guard as the NO/BE/FI
registers. A LEI from a non-Estonian jurisdiction returns `status="unresolved"` in the
coverage report. For the keyless `--ee-file` path, no GLEIF call is made and `lei` is
`null` in every output row.

## CLI usage — EE

```bash
# dry-run (default): parse local elements + metadata CSVs or zips, print summary
company-corpus register-financials --ee-file elements.csv metadata.csv

# dry-run from downloaded zips
company-corpus register-financials \
  --ee-file 4.2024_aruannete_elemendid.zip 1.aruannete_yldandmed.zip

# write data/financials_register/<registrikood>.jsonl + coverage report
company-corpus register-financials \
  --ee-file 4.2024_aruannete_elemendid.zip 1.aruannete_yldandmed.zip --write

# cap to the first N reports (bounded test run)
company-corpus register-financials \
  --ee-file 4.2024_aruannete_elemendid.zip 1.aruannete_yldandmed.zip \
  --limit 1000 --write

# online path: download bulk zips for a given year (explicit URLs required)
company-corpus register-financials --ee-year 2024 \
  --ee-elem-url https://avaandmed.ariregister.rik.ee/…/4.2024_…zip \
  --ee-meta-url https://avaandmed.ariregister.rik.ee/…/1.…zip \
  --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints
entity / period / unbalanced counts without touching disk. `--limit N` caps the number
of reports processed and is available for both `--ee-file` and `--ee-year`.

The `--ee-file` and `--ee-year` flags are mutually exclusive with each other and with
`--orgnrs`, `--leis`, `--ch-bulk`, `--be-file`, `--be-numbers`, `--fi-file`,
`--fi-businessid`, and `--lu-file`.

**URL note:** RIK bulk file names include a rotating snapshot date in the stem
(e.g. `4.2024_aruannete_elemendid_kuni_20250114T180000.zip`). Pass explicit
`--ee-elem-url` and `--ee-meta-url` when using `--ee-year`; obtain the current URLs
from the RIK download listing at
`https://avaandmed.ariregister.rik.ee/et/avaandmete-allalaadimine`.

## Honest caveats — EE

### Leverage is liabilities-based — `interest_coverage` never available

The RIK bulk carries **no financial borrowings element** — no bond, bank-loan, or
lease line is tagged anywhere in the element set (confirmed across the full dataset).
The mapping therefore approximates:

```
short_term_debt  ← CurrentLiabilities    (all current liabilities)
long_term_debt   ← NonCurrentLiabilities (all non-current liabilities)
total_debt       ≈ total liabilities      (not pure financial debt)
```

All gearing metrics (`debt_to_equity`, `debt_to_assets`, `net_debt`, `net_cash`,
`invested_capital`) rest on this total-liabilities basis. This is coarser than the BE
and LU registers (which expose genuine financial borrowings) but is the best available
from the RIK structured data.

As a direct consequence, **`interest_coverage` is never available from this source**:
there is no interest-expense element to form the ratio. This is not a temporary gap —
the bulk simply does not carry that field.

Never compare leverage metrics across registers without checking `source`: a
`debt_to_equity` from `source="rik"` (liabilities-based) is not directly comparable to
one from `source="bnb"` or `source="lbr"` (borrowings-based). The `source` field is
the guard.

### Universe is micro-skewed (~65% dormant or micro)

The RIK avaandmed bulk covers approximately **321 000 registered Estonian companies**.
Of these, roughly **65% are dormant or micro-entities** that file minimal or nil
accounts. For credit-relevance analysis, apply a minimum-assets filter (e.g.
`assets ≥ 1 000 000 EUR`) to narrow to the operating commercial universe. Without such
a filter, aggregate statistics will be heavily distorted by the dormant/micro tail.

### Small economy

Estonia is a small economy (~1.4 million population). The company universe is
correspondingly compact: even after applying a minimum-assets filter, the population of
credit-relevant Estonian entities is modest compared with NO, UK, BE, or LU.

### Annual accounts only, FY 2019–2025

RIK avaandmed holds annual statutory accounts. There are no quarterly or semi-annual
periods. `frequency` is always `"annual"`. History is available from **FY 2019
onwards**; accounts prior to 2019 are not in the avaandmed bulk export.

### Standalone accounts only

The bulk export provides **standalone legal-entity accounts** only. Consolidated group
accounts for Estonian-headquartered groups are not part of the RIK avaandmed dataset.
Every row carries `basis="company"`. Large Estonian groups that also file ESEF
consolidated accounts are handled separately by the EU ESEF pillar and must not be
merged here.

### Estonian GAAP (EAS), not IFRS — never merge with ESEF

RIK statutory accounts follow **Estonian Accounting Standards (EAS)**, which is based
on IFRS but distinct in application and taxonomic detail. Estonian listed groups also
file **ESEF consolidated accounts** under IFRS — those are ingested by the EU ESEF
pillar (`data/financials_eu/`) and must **never be merged** with RIK rows. The GAAP
regime, consolidation scope, and obligor populations all differ.

### No commercial fallback — ever

If a company has no filing in the bulk export, it is recorded as `"no-financials"`.
Revenue, income, and balance-sheet figures are **never supplemented from commercial
databases or any non-public source**. Open data only.

See also: [NO (Brreg)](#source--brreg-regnskapsregisteret-norway),
[UK (Companies House)](#source--uk-companies-house-statutory-accounts-ixbrl),
[BE (BNB CBSO)](#source--belgium-bnb-cbso-annual-accounts-dimensional-xbrl),
[LU (LBR/STATEC)](#source--luxembourg-lbrstatec-centrale-des-bilans-ecdf),
[FI (PRH)](#source--finland-prh-avoindata-xbrl-open-api),
[DK (Erhvervsstyrelsen / Virk)](#source--denmark-erhvervsstyrelsen--virk-regnskaber-xbrl),
[EE (Äriregister/RIK)](#source--estonia-äriregister-rik-avaandmed-bulk-csv),
[SK (registeruz.sk)](#source--slovakia-register-účtovných-závierok-registeruzsk),
[`FINANCIALS.md`](FINANCIALS.md), [`EU_FINANCIALS.md`](EU_FINANCIALS.md).

---

## Source — Slovakia Register účtovných závierok (registeruz.sk)

Slovakia's **Register účtovných závierok** (Register of Financial Statements) is
operated by DataCentrum on behalf of the Ministry of Finance of the Slovak Republic
(MF SR) and makes all public statutory annual accounts available through a
**fully keyless REST API**:

```
GET https://www.registeruz.sk/cruz-public/api/uctovny-vykaz?id=<id>
GET https://www.registeruz.sk/cruz-public/api/sablona?id=<idSablony>
GET https://www.registeruz.sk/cruz-public/api/uctovna-jednotka?id=<id>
GET https://www.registeruz.sk/cruz-public/api/uctovna-zavierka?id=<id>
GET https://www.registeruz.sk/cruz-public/api/uctovne-jednotky?pokracovat-za-id=<cursor>&max-zaznamov=<n>
```

**No API key, no registration, no authentication required** — every endpoint is public
and keyless. The register holds approximately **7.5–8 million public structured reports**
covering statutory accounts from 2014 onwards. Near-universal coverage: every Slovak
legal entity above the filing threshold is required to file annual accounts here.

The traversal path is: paginate `uctovne-jednotky` (entity-ID list) → fetch
`uctovna-jednotka` (entity metadata including IČO) → iterate `uctovna-zavierka` IDs
(one per filing year) → fetch `uctovny-vykaz` (the financial statement) + `sablona`
(the template definition that gives the positional layout). All responses are **plain
JSON**; the parser uses **stdlib `json`** — no XBRL, no Arelle.

**Format: structured positional-array-by-template.** The statement body
(`obsah.tabulky[ti].data`) is a flat string array of numbers. The index formula is:

```
value[col] = data[row_arr_idx * pocetDatovychStlpcov + col]
```

where `pocetDatovychStlpcov` (column count) and the ordered row list are read
**exclusively from the sablona** for table `ti`. The raw vykaz response may carry the
same field in some cases but it is not authoritative. Empty strings parse to `None`;
comma-decimal notation (`"1 051,30"`) is normalised before conversion.

### Two mapped templates

The engine handles exactly two statutory templates:

| Template | ID | Applies to | Assets-table columns |
|---|---|---|---|
| **Úč POD** (regular / large entities) | **699** | Standard Slovak GAAP filers | **4** (brutto / korekcia / netto-current / netto-prior) |
| **Úč MUJ** (micro / small entities) | **687** | Micro-entity and small filers | **2** (current / prior) |

**Column rule.** For POD (699), the assets table (table index 0) carries four data
columns; the meaningful current figure is **netto-current = column index 2**. Every
other table on a POD filing — and every table on a MUJ (687) filing — carries two
columns and the current period is **column index 0**. Reading the wrong column would
silently substitute a gross or prior-year figure; the column rule is therefore part of
the no-false-data correctness heart.

All other templates — including Úč 695 (IFRS reporters) — are suppressed. Large listed
Slovak groups that file under IFRS are ingested by the EU ESEF pillar
(`data/financials_eu/`), not this register.

## Schema — `data/financials_register/<ico>.jsonl`

Output follows the same per-period row model as the other registers and the SEC/EU
pillars (see [`FINANCIALS.md`](FINANCIALS.md) for the full curated-concepts definition):

| Column | Value |
|---|---|
| `entity_id` | IČO (8-digit zero-padded string, e.g. `"36319007"`) |
| `lei` | GLEIF LEI if resolved; `null` for the keyless `--sk-file` path |
| `country` | `"SK"` |
| `source` | `"registeruz"` |
| `basis` | `"company"` (statutory individual accounts) |
| `fy` | Fiscal year (integer, derived from `period_end`) |
| `frequency` | `"annual"` (the register holds only annual statutory accounts) |
| `currency` | `"EUR"` (Slovakia adopted the euro in 2009; all accounts are in EUR) |
| `period_end` | ISO-8601 date string (derived from `titulnaStrana.obdobieDo`) |
| `publication_date` | `null` (not exposed by the registeruz API) |

Each `kind="reported"` row carries the row code as its `tag` (e.g. `"sk:r1"`,
`"sk:r121"`), so downstream consumers can inspect the exact source line.

**POD (699) concept pack** — 13 curated concepts across three tables:

| Curated key | Table | Row | Notes |
|---|---|---|---|
| `assets` | 0 (assets) | r1 | Column 2 (netto-current) — primary gate anchor |
| `non_current_assets` | 0 (assets) | r2 | Column 2 |
| `assets_current` | 0 (assets) | r33 | Column 2 |
| `cash` | 0 (assets) | r71 | Column 2 |
| `equity` | 1 (passives) | r80 | Column 0 |
| `liabilities` | 1 (passives) | r101 | Column 0 |
| `long_term_debt` | 1 (passives) | r121 | Column 0; *Dlhodobé bankové úvery* — real LT bank loans |
| `short_term_debt` | 1 (passives) | r139 | Column 0; *Bežné bankové úvery* — real ST bank loans |
| `revenue` | 2 (P&L) | r2 | Column 0; *Výnosy z hospodárskej činnosti spolu* — **never r1** |
| `operating_income` | 2 (P&L) | r27 | Column 0 |
| `interest_expense` | 2 (P&L) | r49 | Column 0; positive as-is |
| `pretax_income` | 2 (P&L) | r56 | Column 0 |
| `net_income` | 2 (P&L) | r61 | Column 0 |

**MUJ (687) concept pack** — same 13 concepts, all column 0, different row numbers:

| Curated key | Table | Row | Notes |
|---|---|---|---|
| `assets` | 0 (assets) | r1 | Primary gate anchor |
| `non_current_assets` | 0 (assets) | r2 | |
| `assets_current` | 0 (assets) | r14 | |
| `cash` | 0 (assets) | r22 | |
| `equity` | 1 (passives) | r25 | |
| `liabilities` | 1 (passives) | r34 | |
| `long_term_debt` | 1 (passives) | r37 | *Dlhodobé bankové úvery* — real LT bank loans |
| `short_term_debt` | 1 (passives) | r44 | *Bežné bankové úvery* — real ST bank loans |
| `revenue` | 2 (P&L) | r1 | Operating revenues total |
| `operating_income` | 2 (P&L) | r18 | |
| `interest_expense` | 2 (P&L) | r31 | Positive as-is |
| `pretax_income` | 2 (P&L) | r35 | |
| `net_income` | 2 (P&L) | r38 | |

The derived block produced from this pack (for a full filer with both debt lines
present) includes:

- **Aggregates:** `total_debt` (borrowings), `net_debt`, `net_cash`, `working_capital`,
  `invested_capital`
- **Profitability:** `operating_margin`, `net_margin`
- **Returns:** `roe`, `roa`
- **Coverage:** `interest_coverage` (operating income / interest expense)
- **Leverage:** `debt_to_equity` (borrowings), `debt_to_assets` (borrowings)
- **Liquidity:** `current_ratio`, `cash_ratio`
- **Efficiency:** `asset_turnover`

There is no `ebitda` (no depreciation / amortisation concept in the SK pack), no
`effective_tax_rate` (no income-tax line), no `roic` (requires income tax), no
`free_cash_flow`, `cfo_to_debt`, or `fcf_to_debt` (no cash-flow statement), and no
`net_debt_to_ebitda`.

A coverage report is written to `data/reports/register_coverage_registeruz.jsonl` for
every entity processed: `status="ok"` with a period count, `"no-financials"` (no usable
values after gating), `"unbalanced"` (primary balance gate failed), or `"source-error"` (dead source or
parse failure). When items are suppressed, a `suppressed` list records each key and the
reason.

## The confidence gate — no false data

**Governing principle (from `concepts_sk.py`):** a number known to be wrong must
never be emitted; a missing number is strictly better than a wrong one. Two
Slovak-specific traps drive the gate.

### Primary balance gate

The balance-sheet identity differs by template:

- **POD (699):** `|assets − (equity + liabilities + accruals_liab)| ≤ tol`

  The Slovak passive side carries a separate *Časové rozlíšenie* (accruals / deferrals)
  total at r141, sitting between liabilities and equity. The balance identity only
  closes with this accruals term included. A mismatch → the filing is untrustworthy:
  `unbalanced=True`, no values emitted.

- **MUJ (687):** `|assets − (equity + liabilities)| ≤ tol`

  The micro template has no separate accruals total on the passive side.

Tolerance: `tol = max(2 EUR, 0.005 · |assets|)` (0.5 % of assets, never tighter than
2 EUR for micro-entity rounding). Validated to the cent on all 8 real fixtures used in
development.

When any anchor (assets, equity, liabilities) is absent, the gate cannot run and
directly-tagged values still stand — mirroring the BE/FI/DK siblings.

### The net-turnover trap — revenue is operating_revenue_total, never net_turnover

POD r1 (*Čistý obrat*, net turnover) **includes financial income** and therefore does
not represent pure operating sales. The engine maps `revenue` exclusively to the
**operating revenues total** (POD r2 — *Výnosy z hospodárskej činnosti spolu*;
MUJ r1). `net_turnover` is never mapped: on every POD filing the engine records an
explicit suppression for `net_turnover`, making the exclusion auditable in the coverage
report. The distinction is material: on the validated POD fixture the operating revenue
line was 2 005 372 EUR while net turnover was 1 950 307 EUR — a gap driven by financial
income included in the turnover denominator.

### Net-income cross-check

When the equity-section net-income row is available the engine cross-checks it against
the P&L net-income row. A mismatch beyond tolerance causes `net_income` to be
suppressed with the mismatch reason recorded.

## Leverage — borrowings-based (a rich register)

SK is a **rich register for credit analysis**. Unlike Norway (Brreg) and UK (Companies
House) — which expose only broad liabilities aggregates — the registeruz.sk templates
tag **real bank-loan lines** disaggregated by maturity:

| Concept | Template | Row | Slovak label |
|---|---|---|---|
| `long_term_debt` | POD (699) | r121 | *Dlhodobé bankové úvery* |
| `short_term_debt` | POD (699) | r139 | *Bežné bankové úvery* |
| `long_term_debt` | MUJ (687) | r37 | *Dlhodobé bankové úvery* |
| `short_term_debt` | MUJ (687) | r44 | *Bežné bankové úvery* |

Each line is emitted **only when present and nonzero** — these are real bank borrowings,
not total-liabilities aggregates. When both lines are absent or zero (the common case
for micro-entities with no bank debt), the entire debt block is suppressed: no
fabricated `long_term_debt = 0` is written. The producer stamps
`leverage_basis="borrowings"` on every derived leverage row, so the basis is always
identifiable in the output.

**`interest_coverage`** (operating income / interest expense) is available from this
register: the interest-expense line (POD r49 / MUJ r31) is directly tagged in both
templates. This sets SK apart from most registers in this corpus (EE, for instance,
carries no interest element at all).

**Never compare `debt_to_equity` from a SK row to a NO or UK row without first checking
`source` and `leverage_basis`:** `source="registeruz"` is borrowings-based;
`source="brreg"` and `source="companies_house"` are liabilities-based. The `source`
and `leverage_basis` fields are the guards.

## The `basis` field — SK

All SK rows carry `basis="company"`. The registeruz.sk filings are statutory individual
accounts for the Slovak legal entity. The register does not expose consolidated group
accounts; Slovak groups that also file ESEF consolidated accounts under IFRS are handled
separately by the EU ESEF pillar and must not be merged here.

Note that `basis="company"` rows from the registeruz.sk register and
`basis="consolidated"` rows from the EU ESEF pillar (`data/financials_eu/`) are already
separated by output directory. They must **not** be merged without an explicit
accounting and scope reconciliation: Slovak GAAP (based on the Slovak Accounting Act /
*Zákon o účtovníctve*) differs from IFRS in the treatment of deferred tax, provisions,
and lease accounting; and the obligor population (all Slovak legal entities above the
filing threshold) overlaps only partly with the ESEF pillar (Slovak listed groups that
issued regulated securities).

## Identity — IČO and LEI

**IČO (`entity_id`):** The Slovak company identification number (*Identifikačné číslo
organizácie*) — an 8-digit zero-padded numeric string (e.g. `"36319007"`). Non-digit
characters are stripped and the result is left-zero-padded to 8 digits. The IČO is
extracted directly from `vykaz.obsah.titulnaStrana.ico`.

The registeruz.sk API exposes entities by a numeric **entity ID** (an internal
sequential integer, not the IČO). There is **no direct lookup endpoint by IČO**.
Finding a specific company by IČO requires either traversing the entity-ID scan
(building an IČO → entity-ID index offline) or using the `--sk-file` local path when
the vykaz has already been downloaded. The `--sk-id` path takes numeric entity IDs,
not IČOs.

Two input modes:

- **`--sk-file` path:** the IČO is extracted from `vykaz.obsah.titulnaStrana.ico`.
  No entity-ID traversal is needed.
- **`--sk-id` path:** one or more numeric registeruz entity IDs are passed directly.
  The engine traverses entity → zavierka → vykaz for each, **accumulating all available
  periods per IČO** (multi-year, like the NO producer).

**LEI resolution:** When a spec carries a GLEIF LEI, the engine calls
`GET https://api.gleif.org/api/v1/lei-records/{lei}` and extracts
`entity.registeredAs`. This is accepted as the IČO **only when**
`entity.legalAddress.country == "SK"` — the same country guard as the NO/BE/FI/DK/EE
registers. A LEI from a non-Slovak jurisdiction returns `status="unresolved"` in the
coverage report. For the keyless `--sk-file` path, no GLEIF call is made and `lei` is
`null` in every output row.

## CLI usage — SK

```bash
# dry-run (default): parse a local vykaz + sablona JSON pair, print summary
company-corpus register-financials --sk-file sk_36319007_POD.json sk_sablona_699.json

# write data/financials_register/<ico>.jsonl + coverage report
company-corpus register-financials \
  --sk-file sk_36319007_POD.json sk_sablona_699.json --write

# traverse API by entity ID: fetch all periods (dry-run)
company-corpus register-financials --sk-id 12345 67890

# traverse API: multiple entity IDs, bounded batch, persist to disk
company-corpus register-financials --sk-id 12345 67890 --limit 100 --write
```

`--write` is the only side-effecting flag. Omitting it is a safe dry-run that prints
entity / period / unbalanced counts without touching disk. `--limit N` caps the number
of entity IDs processed and is available for `--sk-id` only (no limit for `--sk-file`,
which processes exactly the two supplied files).

The `--sk-file` and `--sk-id` flags are mutually exclusive with each other and with
`--orgnrs`, `--leis`, `--ch-bulk`, `--be-file`, `--be-numbers`, `--fi-file`,
`--fi-businessid`, `--lu-file`, `--dk-file`, `--dk-cvr`, `--ee-file`, and `--ee-year`.

The `--sk-file` path is entirely local (no network). The `--sk-id` path issues GET
requests to `registeruz.sk` for each entity, with per-entity try / except error
isolation (a single failed entity does not abort the batch).

## Honest caveats — SK

### Leverage is borrowings-based — suppressed for zero-debt micro-entities

The SK register provides genuine bank-loan data (POD r121 / r139, MUJ r37 / r44) rather
than total-liabilities aggregates. The derived `total_debt`, `debt_to_equity`,
`debt_to_assets`, and `net_debt` all rest on this borrowings basis — qualitatively the
same level as the BE and LU registers.

**Most micro-entity filers carry no reported bank borrowings.** When both bank-loan
lines are absent or zero the entire debt block — including `debt_to_equity`, `net_debt`,
and `interest_coverage` — is suppressed. For the private and SME universe this is a
common outcome: the absence of leverage metrics signals zero reported bank debt, not
missing data. Apply a minimum-assets filter and check for the presence of
`long_term_debt` or `short_term_debt` rows before computing portfolio-level leverage
averages.

**Never compare `debt_to_equity` from a SK row to a NO or UK row without first
checking `source` and `leverage_basis`:** `source="registeruz"` is borrowings-based;
`source="brreg"` and `source="companies_house"` are liabilities-based.

### Entity-scan endpoint is WAF-throttled

The `uctovne-jednotky` pagination endpoint is WAF-throttled at scale. A politeness
delay of at least 1 second between pages is recommended. Full-register traversal
(millions of entity IDs) requires a sustained crawl with appropriate throttling; this
is a controller-level concern, not exercised in unit tests.

### No direct IČO search — traverse or index

The registeruz.sk API does not expose a per-IČO lookup on the entity-scan endpoint.
Finding a specific entity by IČO requires either traversing the full entity-ID scan
(building an IČO → entity-ID map offline) or using the `--sk-file` local path. The
`--sk-id` path takes numeric entity IDs, not IČOs.

### IFRS filers suppressed — use the ESEF pillar

Large listed Slovak companies that report under IFRS file using template Úč 695.
The registeruz.sk vykaz for IFRS reporters contains **PDF-embedded data only** — no
structured positional tables. These filings are suppressed by the engine
(`idSablony=695 → no-financials`). Their consolidated IFRS figures are available from
the EU ESEF pillar (`data/financials_eu/`).

### Non-public filings suppressed (~25%)

Filings with `pristupnostDat != "Verejné"` are suppressed and recorded as
`no-financials` in the coverage report. Approximately 25% of registeruz.sk filings are
non-public. The structured public universe covers the remaining ~75% of the total
filing count.

### Slovak GAAP statutory — separate from ESEF

SK statutory accounts follow **Slovak GAAP** (based on the Slovak Accounting Act /
*Zákon o účtovníctve*). Slovak listed groups also file **ESEF consolidated accounts**
under IFRS — those are ingested by the EU ESEF pillar (`data/financials_eu/`) and must
**never be merged** with registeruz.sk rows: the GAAP regime, consolidation scope, and
obligor populations all differ.

### Annual accounts only

Slovak statutory accounts are annual. `frequency` is always `"annual"`. There are no
quarterly or semi-annual periods in the registeruz.sk API.

### Small economy — near-universal structured coverage

Slovakia is a small economy (~5.5 million population) but the register provides
**near-universal structured coverage** of the corporate universe from 2014 onwards.
This is qualitatively richer than some larger economies (e.g. UK) where only a fraction
of filings carry structured data. After applying a minimum-assets filter the population
of credit-relevant Slovak entities is modest in absolute terms compared with NO, UK, or
BE, but data quality for the covered universe — full P&L, balance sheet, real bank
borrowings, and interest coverage in a single structured JSON — is high.

### No commercial fallback — ever

If a filing is suppressed (IFRS / non-public / template not mapped) or absent, it is
recorded as `"no-financials"`. Revenue, income, and balance-sheet figures are **never
supplemented from commercial databases or any non-public source**. Open data only.

See also: [NO (Brreg)](#source--brreg-regnskapsregisteret-norway),
[UK (Companies House)](#source--uk-companies-house-statutory-accounts-ixbrl),
[BE (BNB CBSO)](#source--belgium-bnb-cbso-annual-accounts-dimensional-xbrl),
[LU (LBR/STATEC)](#source--luxembourg-lbrstatec-centrale-des-bilans-ecdf),
[FI (PRH)](#source--finland-prh-avoindata-xbrl-open-api),
[DK (Erhvervsstyrelsen / Virk)](#source--denmark-erhvervsstyrelsen--virk-regnskaber-xbrl),
[EE (Äriregister/RIK)](#source--estonia-äriregister-rik-avaandmed-bulk-csv),
[SK (registeruz.sk)](#source--slovakia-register-účtovných-závierok-registeruzsk),
[`FINANCIALS.md`](FINANCIALS.md), [`EU_FINANCIALS.md`](EU_FINANCIALS.md).

---

## Out of scope — future PRs

The current implementation covers **Norway (Brreg)** (JSON, no XBRL), **UK Companies
House** (iXBRL via Arelle, keyless bulk `--ch-bulk` path), **Belgium BNB CBSO**
(dimensional XBRL, stdlib only; keyless `--be-file` + `--be-numbers` API path),
**Luxembourg LBR/STATEC** (custom eCDF XML, stdlib only, keyless `--lu-file` path),
**Finland PRH** (dimensional XBRL, stdlib only; fully keyless `--fi-file` +
`--fi-businessid` paths), **Denmark Erhvervsstyrelsen / Virk** (bare XBRL, stdlib
only; fully keyless `--dk-file` + `--dk-cvr` paths), **Estonia Äriregister/RIK**
(bulk CSV-join, stdlib only; fully keyless `--ee-file` + `--ee-year` paths), and
**Slovakia Register účtovných závierok (registeruz.sk)** (JSON, stdlib only; fully
keyless `--sk-file` + `--sk-id` paths).

For the **UK pillar** specifically, the following are deferred to follow-up PRs:

- **Targeted REST API** (named-entity acquisition with a free Companies House developer
  key — an alternative to bulk for targeted runs)
- **Historic monthly backfill** (iterating the monthly archive to build multi-year
  history per entity)
- **Consolidated-accounts detection** (distinguishing group accounts from
  legal-entity-only accounts within the iXBRL filing)
- **LEI resolution** (populating `lei` for GB entities via GLEIF `registeredAs`)

For the **LU pillar** specifically, the following are deferred to follow-up PRs:

- **Consolidated-model detection** (all rows currently carry `basis="company"`; eCDF
  does not expose a standard consolidation indicator, so distinguishing individual from
  consolidated filings requires an additional filing-type heuristic)
- **LEI resolution on the `--lu-file` path** (the bulk XML carries no LEI data; a
  targeted GLEIF lookup would need to be added at the CLI level)
- **Multi-quarter iteration tooling** (building the full ~14yr history requires
  downloading and iterating multiple quarterly archives; the library accepts multiple
  `--lu-file` paths but the download loop is a maintainer step)

Coverage enrichment for the NO register (e.g. mapping orgnr to LEI for the full Brreg
population) is also deferred.
