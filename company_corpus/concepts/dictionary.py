"""The concept dictionary (chantier 9 spec §6, chantier 10 spec §3).

``concepts.json`` defines each concept once: its meaning. ``tags/<table>.json`` says, for one
taxonomy, which filed tags answer each concept. A new taxonomy is a new tag table: no rule here
names an issuer, a country or a tag. Tags are identified by their full name ``ns:Tag``: the same
bare name in two namespaces is two tags.
"""
from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import resources

STATEMENTS = ("BS", "IS", "CF")
ON_SEVERAL = ("first", "sum")
STATUSES = ("reviewed", "not_reviewed")
KINDS = ("money", "money/shares", "shares")
#: Namespaces a table may take a tag from besides its own: cover-page facts
#: (shares_outstanding falls back to dei:EntityCommonStockSharesOutstanding).
HELPER_NAMESPACES: tuple[str, ...] = ("dei",)
#: Tag tables loaded when none are named, in order; a table is ``tags/<name>.json``.
DEFAULT_TABLES: tuple[str, ...] = ("us_gaap",)
_SEMVER = re.compile(r"\d+\.\d+\.\d+")


class DictionaryError(ValueError):
    """The dictionary files break their own rules."""


@dataclass(frozen=True)
class ComputedTotal:
    """``total_tag`` minus the first concept of ``minus_first_of`` minus the first tag of
    ``minus_first_tag_of``, all read on the balance sheet."""

    total_tag: str
    minus_first_of: tuple[str, ...]
    minus_first_tag_of: tuple[str, ...]


@dataclass(frozen=True)
class ConceptDef:
    """What a concept means, whatever the taxonomy."""

    key: str
    label: str
    statement: str
    instant: bool
    kind: str             # money | money/shares | shares


@dataclass(frozen=True)
class TagEntry:
    """How one taxonomy answers one concept."""

    concept: str
    status: str
    on_several: str
    tags: tuple[str, ...]                 # "ns:Tag", preference order
    statement_only_tags: tuple[str, ...]  # "ns:Tag", count only on the concept's statement
    computed: ComputedTotal | None = None

    @property
    def all_tags(self) -> tuple[str, ...]:
        return (*self.tags, *self.statement_only_tags)


@dataclass(frozen=True)
class TagTable:
    name: str                     # file stem, e.g. "us_gaap"
    taxonomy: str
    namespace: str                # companyfacts namespace, e.g. "us-gaap"
    position_version_prefix: str  # how filing_statements.tag_version names this taxonomy
    entries: dict[str, TagEntry]  # concept key -> entry, in concepts.json order


@dataclass(frozen=True)
class TagSpec:
    """What a dictionary tag must look like to be ingested."""

    ns: str
    tag: str
    instant: bool
    kind: str
    taxonomy_ns: str              # namespace of the table the tag belongs to

    @property
    def full(self) -> str:
        return f"{self.ns}:{self.tag}"


@dataclass(frozen=True, eq=False)  # compared and cached by identity: one object per load
class Dictionary:
    version: str
    conflict_tolerance: float
    restatement_tolerance: float
    concepts: dict[str, ConceptDef]
    tables: tuple[TagTable, ...]

    @property
    def namespaces(self) -> tuple[str, ...]:
        """The taxonomy namespaces of the loaded tables, in table order."""
        return tuple(t.namespace for t in self.tables)

    def table(self, namespace: str) -> TagTable:
        for t in self.tables:
            if t.namespace == namespace:
                return t
        raise KeyError(namespace)

    def entries(self, key: str) -> list[tuple[TagTable, TagEntry]]:
        """The tables answering concept ``key``, in table order."""
        return [(t, t.entries[key]) for t in self.tables if key in t.entries]

    def tag_specs(self) -> dict[str, TagSpec]:
        """Every tag to ingest, chains and helpers alike, by full tag."""
        out: dict[str, TagSpec] = {}

        def add(full: str, instant: bool, kind: str, taxonomy_ns: str) -> None:
            ns, tag = full.split(":", 1)
            out[full] = TagSpec(ns, tag, instant, kind, taxonomy_ns)

        for t in self.tables:
            for e in t.entries.values():
                c = self.concepts[e.concept]
                for full in e.all_tags:
                    add(full, c.instant, c.kind, t.namespace)
                if e.computed:
                    for full in (e.computed.total_tag, *e.computed.minus_first_tag_of):
                        add(full, True, c.kind, t.namespace)
        return out


def _full(tag: str, namespace: str) -> str:
    return tag if ":" in tag else f"{namespace}:{tag}"


def _tolerance(raw: dict, name: str) -> float:
    value = raw.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise DictionaryError(f"{name} {value!r} must be a number >= 0")
    return float(value)


def _parse_concepts(raw: dict) -> dict[str, ConceptDef]:
    out: dict[str, ConceptDef] = {}
    for key, c in (raw.get("concepts") or {}).items():
        if c.get("statement") not in STATEMENTS:
            raise DictionaryError(f"concepts: {key}: unknown statement {c.get('statement')!r}")
        if not isinstance(c.get("instant"), bool):
            raise DictionaryError(f"concepts: {key}: instant must be true or false")
        if c.get("kind") not in KINDS:
            raise DictionaryError(f"concepts: {key}: kind {c.get('kind')!r} is invalid")
        out[key] = ConceptDef(key, c.get("label") or key, c["statement"], c["instant"], c["kind"])
    return out


def _parse_table(name: str, raw: dict, concepts: dict[str, ConceptDef],
                 seen: dict[str, str]) -> TagTable:
    namespace = raw.get("namespace") or ""
    prefix = raw.get("position_version_prefix") or ""
    if not namespace:
        raise DictionaryError(f"{name}: namespace is required")
    if not isinstance(prefix, str) or not prefix:
        raise DictionaryError(f"{name}: position_version_prefix is required")
    allowed = {namespace, *HELPER_NAMESPACES}
    entries: dict[str, TagEntry] = {}
    raw_entries = raw.get("concepts") or {}
    for key in raw_entries:
        if key not in concepts:
            raise DictionaryError(f"{name}: unknown concept {key}")
    for key in concepts:                  # concepts.json order
        if key not in raw_entries:
            continue
        e = raw_entries[key]
        where = f"{name}: {key}"
        if e.get("on_several") not in ON_SEVERAL:
            raise DictionaryError(f"{where}: on_several {e.get('on_several')!r} is invalid")
        if e.get("status") not in STATUSES:
            raise DictionaryError(f"{where}: status {e.get('status')!r} is invalid")
        tags = tuple(_full(t, namespace) for t in e.get("tags") or ())
        only = tuple(_full(t, namespace) for t in e.get("statement_only_tags") or ())
        if not tags and not only:
            raise DictionaryError(f"{where}: no tags")
        computed = None
        if "computed" in e:
            m = e["computed"]
            computed = ComputedTotal(_full(m["total_tag"], namespace), tuple(m["minus_first_of"]),
                                     tuple(_full(t, namespace) for t in m["minus_first_tag_of"]))
            for ref in computed.minus_first_of:
                if ref not in concepts:
                    raise DictionaryError(f"{where}: computed refers to unknown concept {ref}")
        helpers = (computed.total_tag, *computed.minus_first_tag_of) if computed else ()
        for full in (*tags, *only, *helpers):
            ns = full.split(":", 1)[0]
            if full in seen:
                raise DictionaryError(f"{where}: tag {full} already in {seen[full]}")
            if ns not in allowed:
                raise DictionaryError(f"{where}: tag {full} is outside namespace {namespace}")
            seen[full] = where
        entries[key] = TagEntry(key, e["status"], e["on_several"], tags, only, computed)
    return TagTable(name, raw.get("taxonomy") or namespace, namespace, prefix, entries)


def parse_dictionary(concepts_raw: dict, tables_raw: Sequence[tuple[str, dict]]) -> Dictionary:
    """Validate and build; every refusal names the file and the concept at fault."""
    version = concepts_raw.get("version", "")
    if not _SEMVER.fullmatch(version):
        raise DictionaryError(f"version {version!r} is not MAJOR.MINOR.PATCH")
    conflict = _tolerance(concepts_raw, "conflict_tolerance")
    restatement = _tolerance(concepts_raw, "restatement_tolerance")
    concepts = _parse_concepts(concepts_raw)
    seen: dict[str, str] = {}
    tables: list[TagTable] = []
    for name, raw in tables_raw:
        for other in tables:          # before the tags, so the error names the real overlap
            if other.namespace == raw.get("namespace"):
                raise DictionaryError(f"{name}: namespace {other.namespace} already in {other.name}")
            if other.position_version_prefix == raw.get("position_version_prefix"):
                raise DictionaryError(f"{name}: position_version_prefix "
                                      f"{other.position_version_prefix} already in {other.name}")
        tables.append(_parse_table(name, raw, concepts, seen))
    return Dictionary(version, conflict, restatement, concepts, tuple(tables))


_LOADED: dict[tuple[str, ...], Dictionary] = {}


def load_dictionary(tables: Sequence[str] = DEFAULT_TABLES) -> Dictionary:
    """The packaged concepts and the named tag tables, parsed once per table list."""
    key = tuple(tables)
    if key not in _LOADED:
        root = resources.files(__package__)
        concepts = json.loads(root.joinpath("concepts.json").read_text(encoding="utf-8"))
        raws = [(name, json.loads(root.joinpath("tags", f"{name}.json").read_text(encoding="utf-8")))
                for name in key]
        _LOADED[key] = parse_dictionary(concepts, raws)
    return _LOADED[key]
