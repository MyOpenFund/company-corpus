"""Join filings across CIKs with the entity registry (cross-CIK identity).

A single economic entity can span several CIKs (renames, reincorporations,
successors). The `EntityRegistry` maps them, and `discover` fans a single issuer out
to ALL its CIKs via `expand_all`, so e.g. crawling Alphabet also pulls Google's old
CIK. Fully offline (temp registry). Run:

    ./venv/bin/python examples/11_entities.py
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from company_corpus.config import Config
from company_corpus.entity import Entity, EntityRegistry

cfg = Config(data_dir=Path(tempfile.mkdtemp()) / "data")

# Persist the committed alias map. (`add()` would validate it in-memory first,
# failing loudly on a duplicate entity_id or a CIK claimed by two entities.)
EntityRegistry(cfg).save([
    Entity(entity_id="alphabet", name="Alphabet Inc.",
           ciks=["1652044", "1288776"], note="Google -> Alphabet reincorporation"),
])

reg = EntityRegistry(cfg)  # a fresh registry loads it from disk
print("entity for CIK 0001288776:", reg.resolve("1288776").name)
print("expand_all([0001288776]) ->", reg.expand_all(["1288776"]))  # pulls both CIKs of the entity
