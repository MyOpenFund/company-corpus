"""Run a single national OAM backend directly (AMF / France).

Each backend is an `OamSource`: given a resolved `Entity`, `discover()` returns its
`Document`s — and it NEVER raises (the dispatcher wraps it, but any partial failure
is recorded on `src.errors`, never swallowed). Here we resolve Catana Group by ISIN
and run only the French AMF backend. Network (GLEIF + info-financiere.gouv.fr).

    ./venv/bin/python examples/19_eu_discover_one_backend.py
"""
from __future__ import annotations

from collections import Counter

from company_corpus import Config, Fetcher
from company_corpus.eu.entities import resolve_entities
from company_corpus.eu.sources.oam_fr import InfoFinanciereFR

cfg = Config()
fetcher = Fetcher(cfg)

entity = resolve_entities([{"isin": "FR0010193052"}], fetcher=fetcher)[0]   # Catana Group SA
src = InfoFinanciereFR(fetcher=fetcher, config=cfg)
docs = src.discover(entity)

print(f"{entity.name} ({entity.lei}): {len(docs)} documents")
print("  by doc_type:", dict(Counter(d.doc_type for d in docs)))
if docs:
    newest = docs[0]
    print("  newest:", newest.published_ts, "->", newest.doc_type,
          "|", newest.native_meta.get("subtype_of_information", ""))
print("  errors recorded:", [e["context"] for e in src.errors] or "none")
