# The vault contract and RAG ingestion

How `company-corpus` reaches the RAG stack: its manifests **are** rows of the vault's
`documents` contract, the vault ingests them, and
[`data-orchestrator`](https://github.com/MyOpenFund/data-orchestrator) reads company
documents from the vault like central-bank ones. This repo no longer ships a RAG adapter of
its own (the `rag.py` / `rag-items` of the bottom-up era are gone).

## Pipeline

```
company-corpus                         vault (CORPUS=company)          data-orchestrator
  manifest/<cik>.jsonl  (contract)  ─►  checks each row, upserts   ─►  vault --corpus company
  vocabulary.json       (codes)         `documents` (+ vocabulary)      reads COMPANY_CORPUS_ROOT/local_path
  raw/<cik>/...         (files)                                         -> chunk -> embed -> Qdrant
```

The data root (the directory holding `manifest/`, `Config.data_dir`) is the unit a deployment
copies or mounts: every path in a row is relative to it.

## The contract

The `documents` contract **1.0.0** is written and versioned in the vault repo
([`contract/README.md`](https://github.com/MyOpenFund/vault/blob/master/contract/README.md),
package `vault-contract`): one field table for every corpus, refusals (the row is not written)
and quality notices (written, counted). `FilingRecord.to_row()` writes it; the SEC 10-K of the
vault's golden rows is exactly what `to_row()` writes for that filing (pinned by
`tests/contract/`).

| Contract field | An SEC filing |
|---|---|
| `doc_id` | `sha1(cik\|form code\|accession)[:16]`, date-independent (unchanged by the contract) |
| `corpus` | `company` |
| `source_code` | `sec` (`source_codes.SOURCE_CODES`) |
| `doc_type` | taxonomy code (`A1` = 10-K …); meaningful only with `corpus` |
| `title` | the filing title |
| `date`, `year` | the filing date (EDGAR acceptance day) and its year; absent when unknown |
| `date_precision`, `date_kind` | `day`, `publication` — only on a dated row |
| `language` | `en` |
| `provenance` | `edgar_index` \| `edgar_fts` \| `edgar_submissions` \| `wayback` |
| `document_url` | the primary document on EDGAR (`…/aapl-20240928.htm`) |
| `source_url` | the filing index page (`…/<accession>-index.htm`); absent for a non-EDGAR accession |
| `sha256` | hash of the **primary document as the SEC served it** (decomposed from the submission), never of the PDF or text |
| `local_path`, `mime_type` | the RAG artefact (below) |
| `entity_scheme`, `entity_value` | `cik`, the issuer's 10-digit CIK |

Extras (stored by the vault in `extra`): `cik`, `family`, `sec_form`, `accession`,
`period_of_report`, `company` (name as of the filing), `company_current`, `ticker`,
`entity_id`, `primary_path`, `text_path`, `pdf_path`, `submission_path`,
`submission_sha256` (hash of the complete submission), `submission_url`, `alt_urls`,
`frequency`.

- **Entity = the company the document is about**, not its author: a Form 4 in Apple's
  manifest is filed by an officer and is about Apple (`cik` / `0000320193`).
- **Identity in the vault is (`corpus`, `doc_id`)**: a company `A1` (10-K) and a central-bank
  `A1` (rate decision) share a code, not a meaning.
- **Old rows.** `from_row()` also reads the pre-contract names (`form_type`, `filing_date`,
  `primary_doc_url`, `local_path` = submission, `sha256` = submission hash, or the primary's
  hash on a row with no submission). The vault refuses those rows: run `migrate-contract`.

### The artefact rule (`local_path`)

`local_path` is the file the RAG reads: the first artefact the record points at in
`Config.vault_artifact_preference` (default `("pdf", "text")`): the rendered PDF
(`application/pdf`), else the cleaned text (`text/plain`). The engine does not read HTML. The
choice is decided by the pointer being set, never by a disk read, so a row is the same on
every host. A filing with no primary document and no text (discovered but not downloaded, or
a submission with no primary) has no `local_path` and no `mime_type`: it is still a vault row
(inventory), and the RAG counts it as not ingestible.

`local_path` and `sha256` describe different things on purpose: the file the RAG reads, and
the bytes the authority published. A rendered PDF keeps the hash of the HTML it was rendered
from, and an ownership summary (Forms 3/4/5, 13F) that supersedes the decomposed XML on disk
keeps the hash of the XML filed.

### Families: what is a vault document

`Config.vault_families` (default `A, B, C, D, E`) are written to `manifest/`; `save_records`
refuses any other family. **F1 is never a vault document and never a data source**: an F1
summary holds *latest-filed* values (a restatement replaces the original), not what was known
at a date. Its records live in their own index, `derived/f1/<cik>.jsonl` (same row format,
read and written by the F1 code only: `xbrl`, its prune, `verify`); its files stay under
`raw/<cik>/F1/`. Strategies and every point-in-time reader read **the store** (my-open-data
`fundamentals` / `filing_statements`, as of `known_at`; README, "Feeding my-open-data"),
never F1.

### `vocabulary.json`

Written at the data root with every manifest write (and by `migrate-contract`), generated from
the code (`company_corpus/vocabulary.py`): all 23 source codes with their authority, the
`doc_type`s of `vault_families` with label and family, the four SEC provenances, and every
identifier scheme of the source codes (`cik`, `lei`, the register schemes), so the EU and
register rows of a later chantier need no new vocabulary. The vault loads it before the rows
and refuses a row whose codes it does not declare.

## Checking the rows

`vault-contract` is a dev/test dependency only (the runtime never imports it). With it
installed (`pip install -r requirements-dev.txt`), the vault's own checker reads a manifest
directory as the vault ingestion will:

```bash
python -m vault_contract.check data/manifest --corpus company --vocabulary data/vocabulary.json
python -m vault_contract.check data/manifest --corpus company --vocabulary data/vocabulary.json \
    --no-transition --strict          # the final form: no refusal and no notice either
```

Both exit 0 on a migrated corpus. `tests/contract/` checks rows of every family A–E (and
adversarial ones: no primary, no text, undated, a Form 4, a non-EDGAR accession) against the
pinned package.

## Migrating a corpus written before the contract

```bash
python -m company_corpus migrate-contract                 # dry run: counts only
python -m company_corpus migrate-contract --write         # corpus lock, run report
python -m company_corpus migrate-contract --write         # re-run: rows_rewritten=0 f1_moved=0
python -m company_corpus verify --hash                    # 0 findings expected
```

Per issuer, keyed by `doc_id`, local reads only: every row rewritten in the contract, F1 rows
moved to `derived/f1/` (a `doc_id` the index already holds keeps the index's row), the primary
document of each row hashed (from the stored submission, else the primary file),
`vocabulary.json` written. An unreadable line, a stored `doc_id` the identity rule does not
reproduce or a duplicated `doc_id` refuses that whole issuer (its files untouched, an error
names the line). Counts: `rows_rewritten`, `unchanged`, `f1_moved`, `f1_already_indexed`,
`primary_hashed` (a dry run counts the ones a write would hash), `primary_unhashable` (a
pointer and no bytes left), `no_primary`, `no_rag_artifact`, `issuers_refused`.

Until it has run, `verify` reports `f1-in-manifest` and `missing-primary-hash`, and every
command that writes a manifest still holding F1 rows stops (`ManifestNeedsMigration`) rather
than drop them.

## Using the documents

### Citation format
`{company} — {sec_form} ({date}) — {document_url}`, with the chunk's PDF page for
page-anchored references (e.g. *Apple Inc. — 10-K (2024-11-01), p. 42*). Use `company`
(point-in-time, an extra), not `company_current`, so historical filings cite the name in
effect then (e.g. "Facebook Inc" for a 2015 10-K).

### Family weighting (important)
The corpus is volume-skewed: 8-K/6-K (family B) and ownership forms (family E) vastly
outnumber 10-K/10-Q/20-F (family A). For narrative Q&A, ingest families **A and C** by default
and add B/D/E deliberately, or down-sample B/E, so high-count low-narrative filings don't
dominate retrieval.

## Verify it works

```bash
# 1. A tiny corpus
python -m company_corpus discover  --ciks 320193 --forms A1 --years 2024-2025 --download --limit 1
python -m company_corpus render-pdf --ciks 320193 --forms A1 --write   # needs Chrome

# 2. The rows as the vault will judge them (dev requirements installed)
python -m vault_contract.check data/manifest --corpus company --vocabulary data/vocabulary.json

# 3. Ingest through the vault (vault ingestion service with CORPUS=company on a copy of the
#    data root), then in data-orchestrator:
data-orchestrator vault --corpus company --collection zz-company-pilot --limit 5
```
