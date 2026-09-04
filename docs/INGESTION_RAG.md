# RAG ingestion contract

How `company-corpus` feeds the RAG stack (`eigenmind`) via
[`data-orchestrator`](https://github.com/MyOpenFund/data-orchestrator). This
is the company/micro analogue of central-bank-corpus's `INGESTION_RAG.md`.

## Pipeline

```
company_corpus               data-orchestrator               eigenmind
  manifests + data/raw/   ─►   iter_items -> SourceItem  ─►   load -> chunk
                                run_ingest()                  -> embed (MiniLM-384)
                                                              -> Qdrant (cosine)
```

The orchestrator is the adapter: it pulls `SourceItem`s from a source connector,
loads each file, chunks (~500 chars / 80 overlap), embeds, and upserts to Qdrant,
tracking a resume ledger so re-runs skip done docs.

## Ingestion path: **PDF** (Option A)

We feed the **rendered PDF** (`render-pdf` batch) — human-readable, paginated,
and ingestible by the orchestrator's existing PDF loader **with no RAG change**.
The clean `.txt` and primary `.htm` remain on disk; `prefer="text"` is available
as a fallback. Run order:

```bash
python -m company_corpus discover  --universe sp_curated --years 2015-2025 --download
python -m company_corpus render-pdf --universe sp_curated --years 2015-2025 --write   # needs Chrome
```

`render-pdf` requires Chrome/Chromium (set `COMPANY_CORPUS_CHROME` or have it on
PATH). If a PDF is absent for a record, `iter_items(prefer="pdf")` falls back to
the cleaned text so ingestion never silently drops a filing.

## The contract

The orchestrator consumes `SourceItem(doc_id, path, payload)`. This repo yields
exactly that from `company_corpus.rag.iter_items(...)`:

```python
from company_corpus.rag import iter_items

for item in iter_items(root="/path/to/corpus", ciks=["320193"],
                       doctypes="A,C", year_min=2015, year_max=2025, prefer="pdf"):
    item.doc_id   # stable id: sha1(cik|form|accession)[:16]
    item.path     # Path to the .pdf (or .txt fallback)
    item.payload  # metadata dict, merged into every Qdrant chunk
```

### Payload schema (merged into each chunk)

| field | meaning |
|---|---|
| `source` | constant `"company-corpus"` (the product name, hyphenated — distinct from the `company_corpus` module/connector name below, which keeps the underscore for Python import syntax) |
| `doc_id` | stable filing id |
| `cik` | zero-padded CIK (permanent issuer anchor) |
| `company` | issuer name **as of the filing date** (point-in-time) |
| `company_current` | current registrant name (search/joins) |
| `ticker` | current primary ticker |
| `entity_id` | cross-CIK entity id (groups mergers/restructures), if any |
| `doc_type` / `doc_type_label` / `doc_group` | taxonomy code / label / family |
| `sec_form` | raw EDGAR form (`10-K`, `8-K`, …) |
| `year` / `publication_date` / `period_of_report` | filing year / filing date / fiscal period |
| `title`, `url` | filing title, EDGAR primary-doc URL |
| `accession`, `sha256`, `provenance` | EDGAR accession, integrity hash, source |
| `ext`, `rel_path`, `metadata_source` | artifact extension, path rel. to data dir, `"manifest"` |

### Citation format
`{company} — {sec_form} ({publication_date}) — {url}`, with the chunk's PDF page
for page-anchored references (e.g. *Apple Inc. — 10-K (2024-11-01), p. 42*). Use
`company` (point-in-time), not `company_current`, so historical filings cite the
name in effect then (e.g. "Facebook Inc" for a 2015 10-K).

### Family weighting (important)
The corpus is volume-skewed: 8-K/6-K (family B) and ownership forms (family E)
vastly outnumber 10-K/10-Q/20-F (family A). For narrative Q&A, ingest families
**A and C** by default and add B/D/E deliberately, or down-sample B/E, so
high-count low-narrative filings don't dominate retrieval.

## Orchestrator-side connector (ready to paste)

`company_corpus` is not in this repo's push scope, so add this thin shim to
**`data-orchestrator`** at `data_orchestrator/sources/company_corpus.py`
(`company_corpus` must be importable there — `pip install -e` it or put it on
`PYTHONPATH`):

```python
"""company-corpus source connector for data-orchestrator."""
from __future__ import annotations

import os
from collections.abc import Iterator

from data_orchestrator.core import SourceItem
from company_corpus.rag import iter_items as _iter_items


def iter_items(
    root=None, *, ciks=None, doctypes=None,
    year_min=None, year_max=None, prefer="pdf",
) -> Iterator[SourceItem]:
    root = root or os.environ.get("COMPANY_CORPUS_ROOT")
    if isinstance(ciks, str):
        ciks = [c.strip() for c in ciks.split(",") if c.strip()]
    for it in _iter_items(root=root, ciks=ciks, doctypes=doctypes,
                          year_min=year_min, year_max=year_max, prefer=prefer):
        yield SourceItem(doc_id=it.doc_id, path=it.path, payload=it.payload)
```

That's the whole connector — all the corpus logic lives in `company_corpus.rag`,
matching how central-bank-corpus is wired.

## Verify it works

```bash
# 1. Build a tiny corpus
python -m company_corpus discover  --ciks 320193 --forms A1 --years 2024-2025 --download --limit 1
python -m company_corpus render-pdf --ciks 320193 --forms A1 --write   # needs Chrome

# 2. Preview what the RAG would ingest (no orchestrator needed)
python -m company_corpus rag-items --ciks 320193 --prefer pdf

# 3. In data-orchestrator (with the connector above + a local Qdrant):
data-orchestrator company_corpus --ciks 320193 --collection company_corpus
```

Step 2 prints each `SourceItem`'s id, form, year, company, and path — a fast way
to confirm paths and payloads before a full ingest.
