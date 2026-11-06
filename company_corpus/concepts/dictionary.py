"""The concept dictionary: which filed tags mean which concept (chantier 9, spec §6).

Data lives in ``<taxonomy>.json`` next to this module, one file per taxonomy with its own version.
A new taxonomy is a new data file: no rule here names an issuer, a country or a tag.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib import resources

STATEMENTS = ("BS", "IS", "CF")
ON_SEVERAL = ("first", "sum")
STATUSES = ("reviewed", "not_reviewed")
_SEMVER = re.compile(r"\d+\.\d+\.\d+")
_UNIT = re.compile(r"USD|USD/shares|shares")


class DictionaryError(ValueError):
    """The dictionary file breaks its own rules."""


@dataclass(frozen=True)
class ComputedTotal:
    """``total_tag`` minus the first concept of ``minus_first_of`` minus the first tag of
    ``minus_first_tag_of``, all read on the balance sheet."""

    total_tag: str
    minus_first_of: tuple[str, ...]
    minus_first_tag_of: tuple[str, ...]


@dataclass(frozen=True)
class ConceptDef:
    key: str
    label: str
    statement: str
    instant: bool
    unit: str
    status: str
    on_several: str
    tags: tuple[str, ...]                 # "ns:Tag", preference order
    statement_only_tags: tuple[str, ...]  # "ns:Tag", count only on ``statement``
    computed: ComputedTotal | None = None


@dataclass(frozen=True)
class TagSpec:
    """What a dictionary tag must look like to be ingested."""

    ns: str
    tag: str
    instant: bool
    unit: str


@dataclass(frozen=True, eq=False)  # compared and cached by identity: one object per load
class Dictionary:
    version: str
    taxonomy: str
    conflict_tolerance: float
    restatement_tolerance: float
    concepts: dict[str, ConceptDef]

    def tag_specs(self) -> dict[str, TagSpec]:
        """Every tag to ingest, chains and helpers alike, by bare tag name."""
        out: dict[str, TagSpec] = {}

        def add(full: str, instant: bool, unit: str) -> None:
            ns, tag = full.split(":", 1)
            out[tag] = TagSpec(ns, tag, instant, unit)

        for c in self.concepts.values():
            for full in (*c.tags, *c.statement_only_tags):
                add(full, c.instant, c.unit)
            if c.computed:
                for full in (c.computed.total_tag, *c.computed.minus_first_tag_of):
                    add(full, True, c.unit)
        return out


def _full(tag: str, namespace: str) -> str:
    return tag if ":" in tag else f"{namespace}:{tag}"


def parse_dictionary(raw: dict) -> Dictionary:
    """Validate and build; every refusal names the concept at fault."""
    version, namespace = raw.get("version", ""), raw.get("namespace", "")
    if not _SEMVER.fullmatch(version):
        raise DictionaryError(f"version {version!r} is not MAJOR.MINOR.PATCH")
    if not namespace:
        raise DictionaryError("namespace is required")
    for name in ("conflict_tolerance", "restatement_tolerance"):
        tolerance = raw.get(name)
        if not isinstance(tolerance, (int, float)) or isinstance(tolerance, bool) or tolerance < 0:
            raise DictionaryError(f"{name} {tolerance!r} must be a number >= 0")
    concepts: dict[str, ConceptDef] = {}
    seen: dict[str, str] = {}
    raw_concepts = raw.get("concepts") or {}
    for key, c in raw_concepts.items():
        if c.get("statement") not in STATEMENTS:
            raise DictionaryError(f"{key}: unknown statement {c.get('statement')!r}")
        if c.get("on_several") not in ON_SEVERAL:
            raise DictionaryError(f"{key}: on_several {c.get('on_several')!r} is invalid")
        if c.get("status") not in STATUSES:
            raise DictionaryError(f"{key}: status {c.get('status')!r} is invalid")
        if not _UNIT.fullmatch(c.get("unit", "")):
            raise DictionaryError(f"{key}: unit {c.get('unit')!r} is invalid")
        if not isinstance(c.get("instant"), bool):
            raise DictionaryError(f"{key}: instant must be true or false")
        tags = tuple(_full(t, namespace) for t in c.get("tags") or ())
        only = tuple(_full(t, namespace) for t in c.get("statement_only_tags") or ())
        if not tags and not only:
            raise DictionaryError(f"{key}: no tags")
        computed = None
        if "computed" in c:
            m = c["computed"]
            computed = ComputedTotal(_full(m["total_tag"], namespace), tuple(m["minus_first_of"]),
                                     tuple(_full(t, namespace) for t in m["minus_first_tag_of"]))
        for full in (*tags, *only,
                     *((computed.total_tag, *computed.minus_first_tag_of) if computed else ())):
            bare = full.split(":", 1)[1]
            if bare in seen:
                raise DictionaryError(f"{key}: tag {bare} already in {seen[bare]}")
            seen[bare] = key
        concepts[key] = ConceptDef(key, c.get("label") or key, c["statement"], c["instant"],
                                   c["unit"], c["status"], c["on_several"], tags, only, computed)
    for c in concepts.values():
        for ref in c.computed.minus_first_of if c.computed else ():
            if ref not in concepts:
                raise DictionaryError(f"{c.key}: computed refers to unknown concept {ref}")
    return Dictionary(version, raw.get("taxonomy", ""), float(raw["conflict_tolerance"]),
                      float(raw["restatement_tolerance"]), concepts)


_LOADED: dict[str, Dictionary] = {}


def load_dictionary(name: str = "us_gaap") -> Dictionary:
    """The packaged dictionary ``<name>.json``, parsed once."""
    if name not in _LOADED:
        text = resources.files(__package__).joinpath(f"{name}.json").read_text(encoding="utf-8")
        _LOADED[name] = parse_dictionary(json.loads(text))
    return _LOADED[name]
