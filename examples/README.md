# Examples

One runnable script per file, each showing a single piece of `company-corpus`.
Run any of them from the repo root, e.g.:

```bash
# Set a contact so the SEC User-Agent carries one (fair-access politeness).
export COMPANY_CORPUS_CONTACT="you@example.com"

./venv/bin/python examples/01_resolve_universe.py
```

Scripts that hit SEC EDGAR are bounded (one issuer, a filing or two) and write any
artifacts to a temporary directory, so they leave the repo's `data/` untouched.
Several run **fully offline** (no network): `06`, `08`, `11`, `15`, `23`, `24`, `25`, `28`.

Scripts `01`–`15` cover the 🇺🇸 **SEC** pillar; `16`–`24` cover the 🇪🇺 **EU**
pillar; `25`–`29` walk through two of the register-financials pillar's eight
national registers (🇳🇴 Brreg + 🇬🇧 Companies House). See
[`../docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md) for how the pieces
fit together, [`../docs/EU_PILLAR.md`](../docs/EU_PILLAR.md) for the EU design, and
[`../docs/REGISTER_FINANCIALS.md`](../docs/REGISTER_FINANCIALS.md) for the register
pillar.

**Core pipeline**

| Script | Shows |
|---|---|
| `01_resolve_universe.py` | Resolve tickers → CIKs via the official SEC ticker map |
| `02_discover_filings.py` | List an issuer's family-A filings (metadata only) |
| `03_download_and_extract.py` | Download one filing and extract clean RAG-ready text |
| `04_xbrl_financials.py` | Pull XBRL facts → a period summary + derived metrics |
| `05_rag_items.py` | Build a tiny corpus and iterate the `SourceItem`s the RAG ingests |

**Credit-universe resolution** (build a universe from an identifier file)

| Script | Shows |
|---|---|
| `06_universe_from_file.py` | Resolve a CSV of CIK/Ticker/CUSIP/ISIN — authority CIK > CUSIP6 > ticker + collision detection *(offline)* |
| `07_fts_resolution.py` | Reverse-lookup a CUSIP → issuer CIK via EDGAR full-text search (offering forms only) |
| `08_fts_cache.py` | The reusable CUSIP6→CIK cache `--fts-cache` reads/writes (merge + dedup) *(offline)* |
| `09_openfigi_enrich.py` | Enrich + triage ISIN/CUSIP via OpenFIGI (`registry_candidate` vs `private_placement`) |

**Other capabilities**

| Script | Shows |
|---|---|
| `10_ownership.py` | Structure insider Forms 3/4/5 + 13F into normalized rows (family E) |
| `11_entities.py` | Cross-CIK identity — expand one issuer to all its CIKs *(offline)* |
| `12_sp500_historical.py` | Build the S&P 500 as a historical union (`first_seen`/`last_seen`) |
| `13_discover_index.py` | Exhaustive discovery via the quarterly full-index, incl. delisted *(downloads a large index)* |
| `14_completeness_report.py` | Audit coverage with the completeness matrix (ok / partial / missing) |
| `15_name_resolution.py` | Resolve company names → CIKs via the SEC name index (former names, collision detection) *(offline)* |

## 🇪🇺 EU pillar — the "European EDGAR"

Federates national OAMs (AMF, FCA NSM, CONSOB, …) + Euronext + filings.xbrl.org
behind one `acquire()` call, keyed on the GLEIF LEI/ISIN. See
[`../docs/EU_PILLAR.md`](../docs/EU_PILLAR.md) and
[`../docs/EU_BACKENDS.md`](../docs/EU_BACKENDS.md).

| Script | Shows |
|---|---|
| `16_eu_acquire.py` | Bounded multi-country acquisition (FR + ES), with download |
| `17_eu_resolve_identity.py` | Resolve issuers → GLEIF LEI by ISIN and by name; the no-guess `resolution` tier |
| `18_eu_openfigi_bridge.py` | The OpenFIGI ISIN→LEI bridge — resolve issuers GLEIF's ISIN filter misses (`isin-figi`) |
| `19_eu_discover_one_backend.py` | Run one national backend directly (AMF/France) — `discover()` + recorded errors |
| `20_eu_acquire_discovery.py` | End-to-end `acquire()` in discovery mode (dispatch + dedup, no download) |
| `21_eu_coverage_report.py` | Read the per-entity coverage report (`doc_count`, `doc_types`, `gap`) across 3 jurisdictions |
| `22_eu_listing_dispatch.py` | Dispatch by **listing** not home country — cover an issuer with no national OAM via Euronext |
| `23_eu_dedup.py` | Cross-backend dedup — the same ESEF report from two backends collapses to one *(offline)* |
| `24_eu_backends_catalog.py` | The coverage catalog — backends, Euronext markets, doc-type taxonomy *(offline)* |

## Register-financials pillar — statutory accounts from national registers

Open statutory accounts for the private-company universe across eight national
registers — 🇳🇴 Norway (Brreg), 🇬🇧 UK (Companies House), 🇧🇪 Belgium (BNB CBSO),
🇱🇺 Luxembourg (LBR/STATEC eCDF), 🇫🇮 Finland (PRH), 🇩🇰 Denmark
(Erhvervsstyrelsen/Virk), 🇪🇪 Estonia (Äriregister/RIK), and 🇸🇰 Slovakia
(registeruz.sk) — keyed on orgnr / CH number / local entity id. Output goes to
`data/financials_register/` (separate from the EU and SEC pillars). Examples
`25`–`29` below walk through two of the eight (Norway + UK) end to end; see
[`../docs/REGISTER_FINANCIALS.md`](../docs/REGISTER_FINANCIALS.md) for all
eight sources, schema, confidence gate, and caveats.

| Script | Shows |
|---|---|
| `25_register_catalog.py` | Tour of two of the eight sources, concept packs (NO_FIELDS / UK_FIELDS), and output layout *(offline)* |
| `26_no_brreg_financials.py` | Equinor SELSKAP/KONSERN multi-year via Brreg open JSON — revenue / net_income / equity / D/E |
| `27_register_identity.py` | NO/GB identity — direct orgnr, LEI→GLEIF→orgnr, LEI→GLEIF→ch_number, non-NO/GB→unresolved |
| `28_uk_confidence_gate.py` | The four confidence-gate cases: emit / suppress / unbalanced — NO FALSE DATA *(offline)* |
| `29_uk_companies_house_financials.py` | CH iXBRL bulk parse — FRS 105 micro + FRS 102 P&L filer *(needs `.[eu-financials]` Arelle extra)* |
