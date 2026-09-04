"""Cross-CIK entity identity (alias / successor map).

Point-in-time naming (``naming.py``) handles a *single* registrant changing its
name. But some corporate events create or involve **multiple CIKs** for what is
economically one entity:

* holding-company restructures mint a new registrant CIK (e.g. Alphabet's CIK is
  distinct from Google's original CIK), and
* mergers/acquisitions leave a predecessor CIK dormant while filings continue
  under a successor CIK.

EDGAR provides **no** native predecessor/successor link, so — exactly as
cb_corpus does with its alias-aware institution attribution — we keep a small,
version-controlled alias map. Each :class:`Entity` groups the CIKs that belong
to one economic entity under a canonical id/name, so discovery can crawl all of
them and downstream code can join filings across the identity change.

The map lives at ``data/entities/<name>.jsonl`` (committed), one entity per line:

    {"entity_id": "alphabet", "name": "Alphabet / Google",
     "ciks": ["0001652044", "0001288776"], "note": "2015 restructure"}
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config, normalize_cik


@dataclass
class Entity:
    """One economic entity spanning one or more CIKs."""

    entity_id: str
    name: str = ""
    ciks: list[str] = field(default_factory=list)
    note: str = ""

    def __post_init__(self) -> None:
        self.ciks = [normalize_cik(c) for c in self.ciks]

    @property
    def primary_cik(self) -> str | None:
        """The first (canonical) CIK, by convention the current registrant."""
        return self.ciks[0] if self.ciks else None


class EntityRegistry:
    """Load/resolve the committed alias map under ``data/entities/``."""

    def __init__(self, config: Config | None = None, name: str = "aliases"):
        self.config = config or Config()
        self.name = name
        self._by_cik: dict[str, Entity] = {}
        self._by_id: dict[str, Entity] = {}
        self._loaded = False

    @property
    def path(self) -> Path:
        return self.config.data_dir / "entities" / f"{self.name}.jsonl"

    def load(self) -> "EntityRegistry":
        self._by_cik.clear()
        self._by_id.clear()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                self.add(
                    Entity(
                        entity_id=row["entity_id"],
                        name=row.get("name", ""),
                        ciks=row.get("ciks", []),
                        note=row.get("note", ""),
                    )
                )
        self._loaded = True
        return self

    def add(self, entity: Entity) -> None:
        """Register an entity, rejecting conflicts in this trust-root map.

        The alias map is hand-maintained, committed data and is the anchor for
        cross-CIK joins, so a duplicate ``entity_id`` or a CIK claimed by two
        different entities is a data error that must fail loudly rather than
        silently overwrite (last-write-wins) and corrupt attribution.
        """
        if entity.entity_id in self._by_id:
            raise ValueError(
                f"duplicate entity_id {entity.entity_id!r} in alias map {self.name!r}"
            )
        for cik in entity.ciks:
            prior = self._by_cik.get(cik)
            if prior is not None and prior.entity_id != entity.entity_id:
                raise ValueError(
                    f"CIK {cik} is claimed by both {prior.entity_id!r} and "
                    f"{entity.entity_id!r} in alias map {self.name!r}"
                )
        self._by_id[entity.entity_id] = entity
        for cik in entity.ciks:
            self._by_cik[cik] = entity

    def save(self, entities: Iterable[Entity]) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as fh:
            for ent in entities:
                fh.write(json.dumps(asdict(ent), ensure_ascii=False) + "\n")
        return self.path

    def _ensure(self) -> None:
        if not self._loaded:
            self.load()

    def resolve(self, cik: str) -> Entity | None:
        """Return the entity a CIK belongs to (or ``None`` if not aliased)."""
        self._ensure()
        return self._by_cik.get(normalize_cik(cik))

    def entity_id_for(self, cik: str) -> str:
        """Canonical entity id for a CIK; empty string if the CIK is standalone."""
        ent = self.resolve(cik)
        return ent.entity_id if ent else ""

    def expand(self, cik: str) -> list[str]:
        """All sibling CIKs of the entity ``cik`` belongs to (incl. itself).

        A standalone CIK expands to just ``[cik]``.
        """
        ent = self.resolve(cik)
        return list(ent.ciks) if ent else [normalize_cik(cik)]

    def expand_all(self, ciks: Iterable[str]) -> list[str]:
        """Expand a set of CIKs through the alias map, de-duplicated, ordered."""
        seen: set[str] = set()
        out: list[str] = []
        for cik in ciks:
            for expanded in self.expand(cik):
                if expanded not in seen:
                    seen.add(expanded)
                    out.append(expanded)
        return out

    def entities(self) -> Iterator[Entity]:
        self._ensure()
        # Stable, de-duplicated iteration (an entity maps from several CIKs).
        yield from self._by_id.values()
