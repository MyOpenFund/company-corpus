# EU Financials — IFRS structured financials from ESEF (Pillar B)

`company_corpus/eu/financials.py` extracts structured IFRS financial data from
the ESEF (European Single Electronic Format) reports indexed on
**filings.xbrl.org** — the closest European counterpart to the SEC's
`companyfacts` feed — and writes them in the same per-period row schema as the
SEC pillar. This is the reference for what is produced, how, and where coverage
ends.

See also: [`FINANCIALS.md`](FINANCIALS.md) (the SEC/US-GAAP reference that defines
the shared schema and engine) and [`EU_PILLAR.md`](EU_PILLAR.md) (the full EU
filing pillar — discovery, download, and identity resolution).

## Source — filings.xbrl.org OIM JSON

Every ESEF annual report is required by EU regulation to embed its IFRS facts as
machine-readable iXBRL. **filings.xbrl.org** (run by XBRL International) indexes
those reports and exposes each filing's facts as **OIM xBRL-JSON** via a
`json_url` — this is the "European companyfacts". The field is surfaced by the
`FilingsXbrlOrg` backend as a `kind="json_url"` file on each `Document`.

`facts_for_entity` iterates an issuer's filings, fetches each `json_url`, flattens
the facts with `flatten_oim_json`, and unions the resulting points across filings.
Because each annual report typically includes the current and prior-year comparative,
the union yields a multi-year time series; the engine's latest-filed rule resolves
restatements transparently.

## Schema — unified with the SEC pillar

The EU output uses the **same row model** as [`FINANCIALS.md`](FINANCIALS.md):
`kind="reported"`, `kind="derived"`, and `kind="derived_ttm"` rows (see that
document for full definitions of the ~60 curated concepts and all derived metrics).
Both pillars use the same 15-key unified identity/provenance base. For EU rows the
columns take these values:

| Column | EU value | Notes |
|---|---|---|
| `entity_id` | LEI | GLEIF Legal Entity Identifier |
| `id_scheme` | `"lei"` | |
| `lei` | LEI | same as `entity_id` for EU rows |
| `country` | GLEIF jurisdiction | e.g. `"FR"`, `"NL"`, `"GB"` |
| `source` | `"esef"` | literal pillar tag (was: fxo_id in the old schema) |
| `accession` | fxo_id | filings.xbrl.org filing ID (moved here from old `source`) |
| `form` | e.g. `"annual_report"` | replaces old `doc_type` column |
| `sic` | null | no EU SIC equivalent |
| `is_financial` | null | no SIC to classify by |
| `basis` | null | |

All remaining columns (`currency`, `period_end`, `fy`, `frequency`,
`publication_date`, `kind`, `concept`, `label`, `value`, `unit`, `tag`,
`sector_relevant`, …) are identical to SEC rows. EU and SEC rows are directly
comparable in a single analytical table.

Output path: `data/financials_eu/<LEI>.jsonl`

A coverage report is written to `data/reports/eu_financials_coverage.jsonl` for
every entity processed, listing its status (`ok` / `no-financials` / `unresolved`),
the period count, and the fiscal-year range. An issuer absent from filings.xbrl.org
appears as `no-financials` — never a silent drop.

## IFRS concept pack

The `IFRS_CONCEPTS` pack in `company_corpus/xbrl/ifrs_concepts.py` maps the same
curated keys as the SEC pack to `ifrs-full` tags (e.g. `Revenue`,
`ProfitLossFromOperatingActivities`, `CashFlowsFromUsedInOperatingActivities`,
`Equity`). The shared engine (`summaries_from_flat`, `compute_derived`,
`attach_ttm_from_flat`) runs verbatim — the IFRS pack is a drop-in replacement for
the US-GAAP pack.

## Annual-only

ESEF mandates structured iXBRL for **annual reports only**. Half-year reports are
filed in human-readable PDF or HTML; pre-2020 financials predate the ESEF mandate.
Consequently:

- `frequency` is always `"annual"` for EU IFRS rows.
- TTM ratios (`kind="derived_ttm"`) are computed from the annual point only (no
  quarterly roll-up), so they equal the annual derived value — they are retained
  for schema consistency but carry no additional information beyond the annual.
- Half-year and pre-2020 coverage is a **deferred follow-up** (see below).

## Coverage bound

Coverage equals what **filings.xbrl.org** indexes. The main known gaps:

- **Germany (DE): absent.** The Bundesanzeiger does not expose structured ESEF OIM;
  German filers are not indexed on filings.xbrl.org in the `json_url` form, so DE
  returns `no-financials` in the coverage report.
- **Italy (IT): partial.** CONSOB's enforcement of the ESEF mandate has been
  uneven; a subset of Italian issuers appear on filings.xbrl.org, but coverage is
  incomplete relative to the CONSOB register.

Both gaps are **visible** in the coverage report (`data/reports/eu_financials_coverage.jsonl`),
never silent. **Tier B** (Arelle, below) is the path to closing them — it parses the
local ESEF zips for the ESEF-rich backends (Italy especially) and for
**France**. Germany still needs an acquisition-side fix.

## CLI usage

```
company-corpus eu-financials --leis <LEI,...>           # dry-run: print summary, nothing written
company-corpus eu-financials --leis <LEI,...> --write   # write data/financials_eu/<LEI>.jsonl
company-corpus eu-financials --isins <ISIN,...> --write # resolve ISINs first, then write
```

Multiple LEIs or ISINs are comma-separated. `--write` is the only side-effecting
flag; omitting it is a safe dry-run that prints the entity/period count and the
coverage path without touching disk.

### One-issuer live validation

```
./venv/bin/python scripts/validate_eu_financials.py <LEI> --contact you@example.com
```

Prints the entity resolution, how many IFRS concepts mapped, and headline values
(`revenue`, `operating_income`, `net_income`, `assets`, `equity`, `cash`) for the
four most recent annual periods — suitable for eyeballing against the issuer's
published annual report. Pick a large, clean ifrs-full filer indexed on
filings.xbrl.org (check the `json_url` presence in its filings.xbrl.org record
first).

## Tier B — Arelle (optional)

Tier B is an **optional** extension that parses the **local ESEF `.zip` packages**
the national OAM backends download (stored on disk as `kind="esef"` files in the
acquisition manifests) with **Arelle's iXBRL processor** into the same OIM-JSON
facts Tier A uses. The rest of the pipeline — `flatten_oim_json`, `summaries_from_flat`,
`compute_derived`, `attach_ttm_from_flat` — runs identically; Tier B facts are
unioned with Tier A facts before the shared engine runs.

The primary motivation is to **close filings.xbrl.org's coverage gaps**. For
issuers whose ESEF zip is on disk but whose filing is not indexed on
filings.xbrl.org (or whose `json_url` is absent / broken), Arelle can parse the
package directly and recover the IFRS facts.

### Optional dependency

Arelle is **not** installed by the core package. The stdlib-only core is unchanged;
without Arelle, Tier A works normally and calling Tier B raises a clear `ImportError`
with an install hint.

```
pip install ".[eu-financials]"   # adds Arelle
```

### Usage

Tier B is opt-in via the `--arelle` flag. When present, its facts are **unioned**
with Tier A (not a replacement): issuers already indexed on filings.xbrl.org get
the json_url facts (faster); issuers only on disk get the Arelle-parsed facts.

```
# dry-run — prints entity/period summary, nothing written
company-corpus eu-financials --leis <LEI,...> --arelle

# write data/financials_eu/<LEI>.jsonl (Tier A + Tier B unioned)
company-corpus eu-financials --leis <LEI,...> --arelle --write
```

For one-off cross-checks the calibration script parses a single ESEF zip and
optionally compares it to Tier A for the same issuer:

```
./venv/bin/python scripts/calibrate_arelle.py --zip path/to/report.zip [--lei <LEI>] \
    --contact you@example.com
```

### Honest scope — what Tier B covers (and what it does not)

Tier B's **marginal coverage** is issuers whose ESEF zip is already on disk from
ESEF-rich backends but whose filing filings.xbrl.org does not index well. The
backends that download ESEF packages and therefore benefit most are:
**IT, ES, NL, BE, GB, IE, DK, FI, NO** — with Italy being the primary motivation
(CONSOB coverage on filings.xbrl.org is partial; the local zips are more complete).

**France IS covered**: the AMF backend (`oam_fr`) flags its
ESEF report-package `.zip` files as `kind="esef"`, so Tier B parses French annual
reports directly (validated live — e.g. AB Science). The AMF also serves a minority
of **bare `.xhtml`** reports; those stay `document`, because a standalone inline-XBRL
file lacks the bundled extension taxonomy and Arelle resolves no facts from it.

Tier B does **not** yet cover Germany or Sweden:

- **DE:** the Bundesanzeiger backend captures HTML, not ESEF zip packages; there are
  no local zips to parse. Closing DE requires an acquisition-side fix — fetching the
  ESEF packages from the Bundesanzeiger or a third-party aggregator — a **separate
  future PR**.
- **SE:** the Finanscentralen backend tags files `document`; the same
  flag-by-extension fix as FR would likely apply, pending a recon of the SE source.

The coverage report (`data/reports/eu_financials_coverage.jsonl`) records each
entity's `"arelle": true/false` flag so it is always clear which path was used.

### Performance note

Arelle loads the IFRS taxonomy on its **first** call; that download is cached under
Arelle's local cache directory and is not repeated. Subsequent runs start immediately.
Parsing a typical large iXBRL annual report takes tens of seconds. Where a
`json_url` is available on filings.xbrl.org, Tier A's fetch is faster — use
`--arelle` selectively for issuers you know are missing from the aggregator.

---

## Deferred follow-up PRs

Three known gaps remain out-of-scope for the current implementation and are
earmarked for dedicated follow-up PRs:

**1. Acquisition-side fix for DE / SE — enable Tier B for the missing backends.**
The Bundesanzeiger (DE) and the Swedish backend do not currently download
ESEF zip packages into the manifests; without on-disk zips Tier B has nothing to
parse. A dedicated PR would wire the zip acquisition into those backends' fetch
logic, after which Tier B would automatically recover those issuers.

**2. Phase 2 OCR — half-year reports, Switzerland, and pre-2020 financials.**
Half-year reports (PDF) and pre-ESEF filings (pre-2020) are not structured data;
extracting them requires OCR or PDF-table parsing. Swiss issuers have no OAM and
their historical filings predate ESEF entirely. A future OCR-based extraction
phase would extend the time series back and add semi-annual frequency rows.

**3. Register open-data backends — statutory accounts for the credit/private universe. ✅ Complete — 8 registers merged.**
For the credit and private-company universe (non-listed issuers, bond obligors,
bank counterparties) that never file ESEF, statutory accounts filed with national
business registers are the primary source. This is now the
**register-financials pillar** — a separate `data/financials_register/` output,
`basis`-labelled, governed by the no-false-data confidence gate. Eight registers
are merged (PRs #57–66): 🇳🇴 NO Brreg · 🇬🇧 UK Companies House · 🇧🇪 BE BNB ·
🇱🇺 LU LBR · 🇫🇮 FI PRH · 🇩🇰 DK Erhvervsstyrelsen · 🇪🇪 EE Äriregister · 🇸🇰 SK
registeruz. See [`REGISTER_FINANCIALS.md`](REGISTER_FINANCIALS.md).

## Honest limitations

- **Coverage = filings.xbrl.org only.** An issuer not indexed there returns
  `no-financials`; it is recorded, never silently dropped.
- **Annual periods only.** Half-year, quarterly, and pre-2020 coverage requires
  the Phase 2 OCR work.
- **`is_financial` is always `null`** for EU rows: without a SIC code there is no
  automated way to detect financial-sector issuers. The `sector_relevant` flag on
  derived rows therefore defaults to `True` for all EU issuers; consumers of EU
  EBITDA/coverage ratios for banks should apply their own sector filter.
- **Currency is issuer-reported.** Most euro-area issuers report in EUR; UK/CH/SE
  issuers report in GBP/CHF/SEK. The engine filters to the dominant currency per
  filing and never mixes currencies across periods.
