"""``migrate-contract``: rewrite a corpus written before chantier 4 in the vault contract.

One pass per issuer manifest (``manifest/<cik>.jsonl``), keyed by ``doc_id``, local reads only
(no network):

1. every row is read in either form (``FilingRecord.from_row``); a line that is not a record,
   a stored ``doc_id`` the identity rule does not reproduce, or a ``doc_id`` on two lines
   refuses the WHOLE issuer: nothing of it is written, an error item names the line, and the
   operator settles it (``verify`` reports the same rows);
2. F1 rows move to the F1 index (``derived/f1/<cik>.jsonl``); a ``doc_id`` the index already
   holds keeps the index's row (written by the new code, so newer);
3. a vault row with a primary document and no primary hash gets one: from the submission when
   it is on disk (the document as the SEC served it, even where an ownership summary has
   superseded the decomposed file), else from the file at ``primary_path``;
4. the manifest is rewritten in the contract (``FilingRecord.to_row`` with
   ``Config.vault_artifact_preference``) when any row changed, and ``vocabulary.json`` is
   written at the data root.

A dry run reads the manifests only: it counts the primary documents it WOULD hash from the
pointers and the presence of their files, without reading them. A re-run after ``--write``
changes nothing (``rows_rewritten`` 0, ``f1_moved`` 0, ``primary_hashed`` 0).
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, normalize_cik
from .models import FilingRecord, artifact_for_vault
from .storage import Storage, primary_document, read_submission, text_sha256
from .taxonomy import FormType
from .vocabulary import SEC_PROVENANCES, write_vocabulary

#: The counters of a run, in report order.
COUNTERS: tuple[str, ...] = (
    "issuers", "issuers_refused", "rows", "rows_rewritten", "unchanged", "f1_moved",
    "f1_already_indexed", "f1_dropped_tombstoned", "primary_hashed", "primary_unhashable", "no_primary",
    "no_rag_artifact", "provenance_unknown",
)


@dataclass
class MigrationReport:
    """What a migration did (or, in a dry run, would do).

    ``rows_rewritten``/``unchanged``: vault rows whose manifest line changes or not.
    ``f1_moved``: F1 rows leaving ``manifest/`` (``f1_already_indexed`` of them were in the
    index already; ``f1_dropped_tombstoned`` of them were pruned by ``xbrl --write`` since --
    tombstoned in ``f1_pruned.jsonl``, files gone -- and are not restored). ``primary_hashed``: primary hashes computed; ``primary_unhashable``:
    rows with a primary pointer and no bytes left to hash. ``no_primary``: vault rows without a
    primary document; ``no_rag_artifact``: vault rows without a ``local_path`` (the RAG cannot
    ingest them; they stay as inventory). ``provenance_unknown``: vault rows whose provenance
    the vocabulary does not declare (must be 0 before the vault ingests the manifests).
    ``errors``: one item per refused issuer.
    """

    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(COUNTERS, 0))
    errors: list[dict] = field(default_factory=list)
    #: provenance value -> vault rows carrying it, for values ``vocabulary.json`` does not
    #: declare: the vault would refuse those rows (``unknown_provenance``).
    unknown_provenances: dict[str, int] = field(default_factory=dict)

    @property
    def useful_work(self) -> int:
        return self.counts["rows_rewritten"] + self.counts["f1_moved"]


class _Refused(Exception):
    """An issuer the migration must not touch."""


def migrate_contract(config: Config, *, ciks: Sequence[str] | None = None,
                     dry_run: bool = True) -> MigrationReport:
    """Migrate the manifests of ``ciks`` (default: every ``manifest/*.jsonl``)."""
    storage = Storage(config)
    report = MigrationReport()
    tombstones = _tombstones(config)
    for path in _manifest_paths(config, ciks):
        report.counts["issuers"] += 1
        counts = Counter()
        try:
            _migrate_issuer(storage, path, counts, tombstones, dry_run=dry_run)
        except _Refused as exc:
            report.counts["issuers_refused"] += 1
            report.errors.append({"source": "migrate_contract", "context": path.stem,
                                  "error": str(exc)})
            continue
        for key, n in counts.items():
            if key.startswith("provenance:"):
                value = key.split(":", 1)[1]
                report.unknown_provenances[value] = report.unknown_provenances.get(value, 0) + n
                report.counts["provenance_unknown"] += n
            else:
                report.counts[key] += n
    if not dry_run:
        write_vocabulary(config)
    return report


def _manifest_paths(config: Config, ciks: Sequence[str] | None) -> list[Path]:
    if ciks:
        paths = [config.manifest_file(c) for c in dict.fromkeys(normalize_cik(c) for c in ciks)]
        return [p for p in paths if p.is_file()]
    d = config.manifest_dir
    return sorted(p for p in d.glob("*.jsonl") if p.is_file()) if d.is_dir() else []


def _read(path: Path) -> list[tuple[dict, FilingRecord]]:
    """Every row of one manifest with its record; :class:`_Refused` on anything unsafe."""
    try:
        cik = normalize_cik(path.stem)
    except ValueError:
        raise _Refused(f"{path.name}: the file name is not a CIK") from None
    if path.stem != cik:
        raise _Refused(f"{path.name}: the file name is not the canonical CIK {cik}")
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError) as exc:
        raise _Refused(f"{path.name}: cannot be read ({exc})") from exc
    out: list[tuple[dict, FilingRecord]] = []
    seen: dict[str, int] = {}
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        where = f"{path.name}:{lineno}"
        try:
            row = json.loads(line)
            rec = FilingRecord.from_row(row)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise _Refused(f"{where}: not a filing record ({type(exc).__name__}: {exc}); "
                           "fix or remove the line, then re-run") from exc
        stored = row.get("doc_id")
        if stored != rec.doc_id:
            raise _Refused(f"{where}: stored doc_id {stored!r} is not the identity rule's "
                           f"{rec.doc_id} ({rec.form_type.code} {rec.accession}); the "
                           "migration never changes an identity (see `verify`)")
        if normalize_cik(rec.cik) != cik:
            raise _Refused(f"{where}: a row of cik {rec.cik} in {cik}'s manifest")
        if rec.doc_id in seen:
            raise _Refused(f"{where}: duplicate doc_id {rec.doc_id} (also line "
                           f"{seen[rec.doc_id]}); keep one line, then re-run")
        seen[rec.doc_id] = lineno
        out.append((row, rec))
    return out


def _tombstones(config: Config) -> frozenset[str]:
    """doc_ids of the F1 records ``xbrl --write`` pruned (``f1_pruned.jsonl``)."""
    try:
        lines = config.f1_prune_log_path.read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeDecodeError):
        return frozenset()
    ids = set()
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("doc_id"), str):
            ids.add(row["doc_id"])
    return frozenset(ids)


def _pruned_since(config: Config, rec: FilingRecord, tombstones: frozenset[str]) -> bool:
    """An F1 row still in an old manifest that the new code's prune already removed: its
    doc_id is tombstoned and none of its files is left. Restoring it into the index would
    bring back a record the run decided was stale, pointing at nothing."""
    if rec.doc_id not in tombstones:
        return False
    pointers = [p for p in (rec.primary_path, rec.text_path, rec.pdf_path) if p]
    return not any((config.data_dir / p).exists() for p in pointers)


def _migrate_issuer(storage: Storage, path: Path, counts: Counter,
                    tombstones: frozenset[str] = frozenset(), *, dry_run: bool) -> None:
    config = storage.config
    rows = _read(path)
    cik = path.stem
    allowed = set(config.vault_families)
    vault: list[tuple[dict, FilingRecord]] = []
    f1: list[FilingRecord] = []
    for row, rec in rows:
        if rec.form_type is FormType.F1:
            f1.append(rec)
        elif rec.form_type.family in allowed:
            vault.append((row, rec))
        else:
            raise _Refused(f"{path.name}: a {rec.form_type.code} row ({rec.doc_id}) is outside "
                           f"Config.vault_families {config.vault_families}")

    counts["rows"] += len(rows)
    preference = config.vault_artifact_preference
    changed = bool(f1)
    for row, rec in vault:
        would_hash = False
        if rec.primary_path and not rec.primary_sha256:
            if dry_run:
                ok = would_hash = _hash_source_exists(config, rec)
            else:
                rec.primary_sha256 = _primary_hash(config, rec)
                ok = rec.primary_sha256 is not None
            counts["primary_hashed" if ok else "primary_unhashable"] += 1
        if not rec.primary_path:
            counts["no_primary"] += 1
        if artifact_for_vault(rec, preference)[0] is None:
            counts["no_rag_artifact"] += 1
        if rec.provenance not in SEC_PROVENANCES:
            counts[f"provenance:{rec.provenance}"] += 1
        new = rec.to_row(artifact_preference=preference)
        if would_hash:
            # The hash a write would compute is unknown here: the row changes.
            new = None
        if new == row:
            counts["unchanged"] += 1
        else:
            counts["rows_rewritten"] += 1
            changed = True

    indexed = storage.load_f1(cik) if f1 else {}
    counts["f1_moved"] += len(f1)
    counts["f1_already_indexed"] += sum(1 for r in f1 if r.doc_id in indexed)
    dropped = {r.doc_id for r in f1
               if r.doc_id not in indexed and _pruned_since(config, r, tombstones)}
    counts["f1_dropped_tombstoned"] += len(dropped)
    if dry_run or not changed:
        return
    if f1:
        # The index first: a crash between the two writes leaves F1 rows in both places,
        # which a re-run settles (the index's row wins), never in neither.
        merged = {r.doc_id: r for r in f1 if r.doc_id not in dropped}
        merged.update(indexed)
        storage._write_f1(cik, merged.values())
    storage._write_manifest(cik, [rec for _, rec in vault])


def _hash_source_exists(config: Config, rec: FilingRecord) -> bool:
    return any(p and (config.data_dir / p).is_file()
               for p in (rec.submission_path, rec.primary_path))


def _primary_hash(config: Config, rec: FilingRecord) -> str | None:
    """The primary document's hash from the files on disk (see the module docstring)."""
    if rec.submission_path:
        try:
            raw = read_submission(config.data_dir / rec.submission_path)
        except (OSError, UnicodeDecodeError):
            raw = None
        if raw is not None:
            primary = primary_document(rec, raw)
            if primary and primary.text:
                return text_sha256(primary.text)
    try:
        return hashlib.sha256((config.data_dir / rec.primary_path).read_bytes()).hexdigest()
    except OSError:
        return None
