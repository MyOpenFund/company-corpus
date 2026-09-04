"""Build a tiny corpus and iterate the SourceItems the RAG would ingest.

`iter_items` walks the manifests and yields `SourceItem(doc_id, path, payload)`
straight to data-orchestrator. Here we discover + download one filing into a
temp corpus, then preview what ingestion would see. Run:

    ./venv/bin/python examples/05_rag_items.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from company_corpus import (
    Config,
    discover_universe,
    download_universe,
    iter_items,
    parse_scope,
)

APPLE_CIK = "320193"

cfg = Config(data_dir=Path(tempfile.mkdtemp(prefix="bottomup_example_")))
scope = parse_scope("A1")  # 10-K only, to keep it to one download

discover_universe([APPLE_CIK], scope=scope, dry_run=False, config=cfg)
download_universe([APPLE_CIK], scope=scope, dry_run=False, limit=1, config=cfg)

# prefer="text" because we haven't rendered PDFs (render-pdf needs Chrome).
for item in iter_items(root=cfg.data_dir, ciks=[APPLE_CIK], prefer="text", config=cfg):
    p = item.payload
    print(f"doc_id={item.doc_id}")
    print(f"  {p['company']} — {p['sec_form']} ({p['publication_date']})")
    print(f"  cik={p['cik']}  form={p['doc_type']}  year={p['year']}")
    print(f"  url={p['url']}")
    print(f"  path={item.path}")
