"""Download one filing's complete submission and extract clean, RAG-ready text.

`Storage.fetch_and_store` downloads the SGML complete-submission, decomposes the
primary document out of it, and writes the cleaned plaintext — all three layered
artifacts. Writes to a temp dir, so the repo's data/ is untouched. Run:

    ./venv/bin/python examples/03_download_and_extract.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from company_corpus import Config, Fetcher, Storage, parse_scope
from company_corpus.sources.edgar_submissions import EdgarSubmissions

APPLE_CIK = "320193"

cfg = Config(data_dir=Path(tempfile.mkdtemp(prefix="bottomup_example_")))
fetcher = Fetcher(cfg)
storage = Storage(cfg)

# Grab Apple's most recent 10-K (scope A1), then download + decompose it.
records = list(EdgarSubmissions(fetcher, cfg).discover(APPLE_CIK, scope=parse_scope("A1")))
latest_10k = max(records, key=lambda r: r.filing_date)
print(f"Downloading {latest_10k.sec_form} filed {latest_10k.filing_date} …")

result = storage.fetch_and_store(latest_10k, fetcher, dry_run=False)
print(f"  status={result.status}  bytes={result.bytes:,}  sha256={latest_10k.sha256[:16]}…")
print(f"  submission: {latest_10k.local_path}")
print(f"  primary   : {latest_10k.primary_path}")
print(f"  clean text: {latest_10k.text_path}")

snippet = (cfg.data_dir / latest_10k.text_path).read_text(encoding="utf-8")[:300]
print("\n--- first 300 chars of extracted text ---\n" + snippet)
