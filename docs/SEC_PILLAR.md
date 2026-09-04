# 🇺🇸 SEC pillar — guide

The anchor pillar: every U.S. filing from **SEC EDGAR** (public-domain open data),
keyed on the issuer's permanent **CIK**. For the architecture and lifecycle see
[`ARCHITECTURE.md`](ARCHITECTURE.md); this is the practical guide (taxonomy,
storage, CLI).

## Filing taxonomy (families, analog of cb_corpus A–G)

| Family | Codes | Forms |
|---|---|---|
| A. Periodic reports | A1–A4 | 10-K, 10-Q, 20-F, 40-F |
| B. Current / material events | B1–B2 | 8-K, 6-K (incl. earnings-release exhibits) |
| C. Governance | C1–C2 | DEF 14A, other proxy |
| D. Registration / offering | D1–D3 | S-1, S-4, 424B |
| E. Ownership (structured, opt-in) | E1–E3 | Forms 3/4/5, 13F, SC 13D/G |
| F. Structured financials (opt-in) | F1 | XBRL company facts / financial statement datasets |

Default crawl scope (`FULL_SCOPE`) is the narrative families **A–D**; E and F are
opt-in.

## Storage layout & file naming

```
data/
├── manifest/<cik>.jsonl        # INDEX: one JSON line per filing (committed) — the map
├── universe/<name>.jsonl       # curated issuer lists, ticker<->cik (committed)
│   └── <name>_changes.jsonl    # dated index changes (e.g. sp500_changes.jsonl)
├── financials/<cik>.jsonl      # normalized XBRL metrics, one row per metric (committed)
├── ownership/<cik>.jsonl       # normalized insider/13F rows (committed)
├── discovery_errors.jsonl      # append-only audit trail
├── reports/                    # completeness matrices, CSV exports
└── raw/<cik>/<code>/<year>/    # the actual documents (git-ignored, regenerable)
```

The layout is *machine-first* (stable ids, short codes), so **use the manifest as
your entry point — don't browse `raw/` by hand**: every manifest line already
contains the exact file paths.

### The path `raw/<cik>/<code>/<year>/<doc_id>.<type>`

- **`<cik>`** — SEC Central Index Key, **zero-padded to 10 digits** (Apple =
  `0000320193`). Permanent (never changes on rename); also the manifest filename.
- **`<code>`** — the **internal family code** (not the raw SEC form):

  | Code | Form | | Code | Form |
  |---|---|---|---|---|
  | A1 | 10-K | | C1 | DEF 14A |
  | A2 | 10-Q | | C2 | other proxy |
  | A3 | 20-F | | D1/D2/D3 | S-1 / S-4 / 424B |
  | A4 | 40-F | | E1 | Form 3/4/5 (insider) |
  | B1 | 8-K | | E2 | 13F (holdings) |
  | B2 | 6-K | | E3 | SC 13D/G |
  |  |  | | F1 | XBRL financials |

- **`<year>`** — filing year (`filing_date`).
- **`<doc_id>`** — `sha1(cik | code | accession)` truncated to 16 hex. **Stable**
  (date corrections don't change it) and unique per filing; the shared stem of all
  that filing's files.

### Files for one filing (shared `<doc_id>`)

| Suffix | Contents |
|---|---|
| `<doc_id>.submission.txt` | the **complete submission** (SGML: primary doc + all exhibits + XBRL) — canonical archive |
| `<doc_id>.primary.{htm,html,xml}` | the decomposed **primary document** (or the generated summary for F1/E) |
| `<doc_id>.txt` | the cleaned **extracted text** (what the RAG reads) |
| `<doc_id>.pdf` | the rendered **PDF** (after `render-pdf`) |

Special cases: **F1** (financials) has no submission — instead
`raw/<cik>/F1/companyfacts.json` (raw XBRL, one per issuer) plus a generated
`.primary.html`/`.txt` summary per period. **E1/E2** (ownership) download the
submission, then replace the primary/text with a structured summary.

### The manifest is the map

`data/manifest/<cik>.jsonl` — one JSON object per filing, carrying the metadata
**and the exact file paths** so you never decode a hash by hand:

```jsonc
{
  "cik":"0000320193", "ticker":"AAPL",
  "company":"Apple Inc.",          // name as of the filing date (point-in-time)
  "company_current":"Apple Inc.", "entity_id":"",
  "form_type":"A1", "family":"A", "sec_form":"10-K",   // internal code + raw form
  "accession":"0000320193-25-000079", "title":"Apple Inc. 10-K ...",
  "filing_date":"2025-10-31", "period_of_report":"2025-09-27", "year":2025,
  "primary_doc_url":"https://www.sec.gov/Archives/edgar/.../aapl-...htm",
  "submission_url":"...0000320193-25-000079.txt",
  "sha256":"…", "provenance":"edgar_submissions",
  "local_path":"raw/0000320193/A1/2025/<doc_id>.submission.txt",
  "primary_path":"raw/0000320193/A1/2025/<doc_id>.primary.htm",
  "text_path":"raw/0000320193/A1/2025/<doc_id>.txt",
  "pdf_path":"raw/0000320193/A1/2025/<doc_id>.pdf",
  "doc_id":"<16-hex>"
}
```

### Navigation recipes

```bash
grep -i '"AAPL"' data/universe/sp500.jsonl                                    # ticker -> CIK
jq -r 'select(.sec_form=="10-K") | "\(.year)  \(.text_path)"' data/manifest/0000320193.jsonl
ls data/raw/0000320193/A1/2025/<doc_id>.*                                     # all files of one filing
```

## CLI

### Global options (before the subcommand)

```bash
python -m company_corpus --data-dir /data/corpus --contact you@example.com discover ...
```

- `--data-dir PATH` — corpus root (default `./data`).
- `--contact EMAIL` — contact for the SEC `User-Agent`; overrides `$COMPANY_CORPUS_CONTACT`.
- `--insecure` — disable TLS verification. **Only** behind a trusted SSL-inspection proxy.

```bash
python -m company_corpus list-forms              # taxonomy (filter: --forms A)
python -m company_corpus config                  # effective runtime config
```

### Issuer universe (curated, version-controlled under `data/universe/`)

```bash
# tickers -> CIKs via the official SEC map (dry-run, then --write)
python -m company_corpus build-universe --tickers AAPL,MSFT,GOOGL --name sp_curated --write
python -m company_corpus list-universe --name sp_curated

# S&P 500 from its dated composition (the only equity index with open dated history)
python -m company_corpus build-universe --equity-index sp500 --current-only --write   # today's ~500
python -m company_corpus build-universe --equity-index sp500 --since 2010 --write      # historical UNION

# From a CSV of identifiers (credit-index constituents or any list); authority CIK > CUSIP > ticker
python -m company_corpus build-universe --from-file credit_universe.csv \
    --crosswalk cik-cusip-maps.csv --name credit_demo --write
```

`--equity-index sp500` reconstructs membership from Wikipedia (current + dated
changes), so the historical union is **not survivorship-biased on selection**.
Current members get CIKs; since-delisted ones are kept with `cik=""` and reported.
Russell 1000 / Nasdaq-100 have no open dated source (unsupported). (`--index` is a
deprecated alias.)

**Credit indices** are proprietary → bring constituents via `--from-file`. The
`CIK` column is authoritative; otherwise each row resolves by CUSIP6→CIK (needs
`--crosswalk`, a `cik,cusip6,cusip8` CSV) and/or ticker→CIK. When the two derived
sources disagree (a recycled ticker pointing elsewhere than the bond's CUSIP6) the
row is a **collision** (`name_match`/`name_mismatch`, written to
`data/universe/<name>_collisions.jsonl`) — kept resolved to `--prefer` (default
`cusip`), or excluded with `--drop-collisions`.

**Name→CIK resolution (on by default).** When ticker *and* CUSIP miss a
name-bearing row, `build-universe` resolves it by *name* against the SEC
`cik-lookup-data.txt` file (one cached download, former names included) —
recovering delisted/renamed members (the ~139 historical S&P 500 names the current
ticker map drops). Matching is exact after strict normalization; a name borne by
two companies is a collision, broken by dated `formerNames` when a membership date
is known. Decisions accumulate in `data/reference/name_cik_cache.csv`
(`--name-cache`). Disable with `--no-name-resolution`.

For names no offline tier resolves, `--fts` adds an opt-in network tier: EDGAR
full-text search on the bond's CUSIP, **restricted to offering forms**
(424B/FWP/S-3) so the hit is the issuer not a fund holder; each is name-corroborated
as `fts:confirmed`/`fts:unverified`. `--fts-limit N` bounds it; `--fts-cache FILE`
(a `cik,cusip6` CSV) makes it durable (read into the crosswalk, confirmed pairs
appended back).

**OpenFIGI enrichment** (triage, not resolution) — labels *why* an issuer is
unresolved; jurisdiction-neutral, reusable:

```bash
python -m company_corpus enrich-openfigi --from-file ids.csv --id-type isin --out enriched.csv
# -> identifier,name,ticker,security_type,exch_code,coverage_hint
```

`coverage_hint`: `registry_candidate` (publicly registered), `private_placement`
(144A/Reg-S — in no public registry), or `unknown`. A free key (`--api-key` /
`$OPENFIGI_API_KEY`) raises rate limits; works without one.

### Discovery (metadata into per-issuer manifests; dry-run by default, `--write` to persist)

```bash
python -m company_corpus discover --universe sp_curated --years 2006-2025          # dry-run
python -m company_corpus discover --universe sp_curated --years 2006-2025 --write --rounds 3
python -m company_corpus discover --ciks 320193 --forms A,B,C,D,E --write          # specific CIKs / wider scope
python -m company_corpus discover --universe sp_curated --download --since 2015-01-01 --write
python -m company_corpus discover-index --universe sp_curated --years 2006-2025 --write  # exhaustive (incl. delisted)
```

### Company identity (rename / rebrand / merger)

A company's **CIK is permanent** — it never changes on a rename — so everything is
keyed on CIK. On top of that anchor:

- **Point-in-time naming.** Each filing is attributed to the name in effect on its
  filing date (EDGAR `formerNames`); the current name is kept in `company_current`.
  A 2015 filing reads `Facebook Inc`; a 2023 one `Meta Platforms, Inc.` — same CIK.
- **Cross-CIK entities.** Some events (holding-company restructures, mergers) span
  multiple CIKs EDGAR doesn't link (e.g. Alphabet 1652044 ← Google 1288776). A
  committed alias map (`data/entities/aliases.jsonl`) groups them; discovery expands
  through it and stamps `entity_id`.
  ```bash
  python -m company_corpus entities                 # list grouped entities
  python -m company_corpus entities --cik 1288776   # resolve a CIK
  ```
- **Survivorship.** `company_tickers.json` lists *current* issuers only — a
  ticker-built universe omits delisted/merged ones. Anchor on CIK or crawl the full
  index for history:
  ```bash
  python -m company_corpus build-universe --ciks 1288776,320193 --name historical --write
  ```

### Download, render, financials, ownership

```bash
# download + decompose (submission -> primary -> cleaned text); filter by year/dates
python -m company_corpus download --universe sp_curated --years 2010-2020 --write
python -m company_corpus download --universe sp_curated --since 2015-01-01 --until 2019-12-31 --write

# render PDFs (needs Chrome via COMPANY_CORPUS_CHROME or PATH) + preview RAG items
python -m company_corpus render-pdf --universe sp_curated --years 2015-2025 --write
python -m company_corpus rag-items  --universe sp_curated --prefer pdf

# completeness matrix (discovered vs expected per issuer/form/year)
python -m company_corpus report --universe sp_curated --years 2015-2025 --csv data/reports/matrix.csv

# structured XBRL financials (F1) — see docs/FINANCIALS.md
python -m company_corpus xbrl --universe sp_curated --years 2015-2025 --write

# ownership (E) — structured insider transactions + 13F holdings
python -m company_corpus discover  --universe sp_curated --forms E --write
python -m company_corpus ownership --universe sp_curated --write
```

`ownership` parses **Form 3/4/5** (E1) and **13F** (E2) XML into readable summaries
+ a normalized `data/ownership/<cik>.jsonl` table; **SC 13D/G** (E3) pass through
as text. Bounded to the curated tier by default (Form 4 alone is ~4.6M filings);
`--limit` caps a run. XBRL financials are detailed in
[`FINANCIALS.md`](FINANCIALS.md).

## Feeding the RAG

`company_corpus.rag.iter_items()` yields `SourceItem(doc_id, path, payload)`
straight from the manifests (PDF by default, text fallback). Full contract +
connector in [`INGESTION_RAG.md`](INGESTION_RAG.md).
