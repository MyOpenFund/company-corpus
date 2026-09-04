"""RAG ingestion adapter.

Bridges this corpus to the RAG stack consumed via ``data-orchestrator``. The
orchestrator iterates ``SourceItem(doc_id, path, payload)`` objects, loads each
file, chunks + embeds it, and upserts to Qdrant. This module yields exactly that
shape from our per-issuer manifests, so the orchestrator-side connector
(``data_orchestrator/sources/company_corpus.py``) is a thin shim — keeping the
corpus knowledge here, with the corpus.

The :class:`SourceItem` dataclass mirrors the orchestrator's (same fields), so a
shim can re-yield ours directly or map them 1:1.

Default ``prefer="pdf"`` matches the chosen ingestion path (Option A): the
human-readable, page-anchored PDF produced by the ``render-pdf`` batch is fed to
the RAG. ``"text"`` / ``"primary"`` are available as fallbacks.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, normalize_cik
from .models import FilingRecord
from .taxonomy import FormType, parse_scope

# Which stored artifact to feed, in fallback order.
_PREFERENCE = {
    "pdf": ("pdf_path", "primary_path", "text_path"),
    "text": ("text_path", "primary_path"),
    "primary": ("primary_path", "text_path"),
}


@dataclass
class SourceItem:
    """Mirror of the orchestrator's SourceItem (doc_id, path, payload)."""

    doc_id: str
    path: Path
    payload: dict = field(default_factory=dict)


def _select_path(record: FilingRecord, prefer: str, data_dir: Path) -> Path | None:
    for attr in _PREFERENCE.get(prefer, _PREFERENCE["pdf"]):
        rel = getattr(record, attr, None)
        if rel:
            candidate = data_dir / rel
            if candidate.exists():
                return candidate
    return None


def _payload(record: FilingRecord, path: Path, data_dir: Path) -> dict:
    return {
        "source": "company-corpus",
        "doc_id": record.doc_id,
        "cik": record.cik,
        "company": record.company,                 # point-in-time (as filed)
        "company_current": record.company_current,
        "ticker": record.ticker,
        "entity_id": record.entity_id,
        "doc_type": record.form_type.code,
        "doc_type_label": record.form_type.label,
        "doc_group": record.form_type.family,
        "sec_form": record.sec_form,
        "year": record.year,
        "publication_date": record.filing_date.isoformat() if record.filing_date else "",
        "period_of_report": record.period_of_report.isoformat() if record.period_of_report else "",
        "title": record.title,
        "url": record.primary_doc_url,
        "accession": record.accession,
        "sha256": record.sha256 or "",
        "provenance": record.provenance,
        "ext": path.suffix.lstrip("."),
        "rel_path": str(path.relative_to(data_dir)),
        "metadata_source": "manifest",
    }


def _manifest_ciks(config: Config) -> list[str]:
    d = config.manifest_dir
    return sorted(p.stem for p in d.glob("*.jsonl")) if d.exists() else []


def iter_items(
    root: str | Path | None = None,
    *,
    ciks: Sequence[str] | None = None,
    doctypes: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    prefer: str = "pdf",
    config: Config | None = None,
) -> Iterator[SourceItem]:
    """Yield :class:`SourceItem`s for ingestion, from per-issuer manifests.

    Selection: ``ciks`` (or all manifests), filtered by ``doctypes`` (family/code
    selector) and inclusive ``year_min``/``year_max``. ``prefer`` chooses which
    stored artifact to feed (``pdf`` default; falls back when absent). Records
    whose chosen artifact is not on disk are skipped (they need download/render
    first).
    """
    config = config or (Config(data_dir=Path(root)) if root else Config())
    data_dir = config.data_dir
    scope: set[FormType] | None = set(parse_scope(doctypes)) if doctypes else None
    target_ciks = [normalize_cik(c) for c in ciks] if ciks else _manifest_ciks(config)

    manifest_dir = config.manifest_dir
    for cik in target_ciks:
        path = manifest_dir / f"{cik}.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            record = FilingRecord.from_row(json.loads(line))
            if scope is not None and record.form_type not in scope:
                continue
            if year_min is not None and (record.year is None or record.year < year_min):
                continue
            if year_max is not None and (record.year is None or record.year > year_max):
                continue
            artifact = _select_path(record, prefer, data_dir)
            if artifact is None:
                continue  # not downloaded/rendered yet
            yield SourceItem(
                doc_id=record.doc_id,
                path=artifact,
                payload=_payload(record, artifact, data_dir),
            )
