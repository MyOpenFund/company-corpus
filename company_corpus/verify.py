"""Read-only integrity self-check over a real corpus directory.

Reports; never repairs, never deletes. It exists because the convergence bugs
this chantier closes were invisible from the inside: a manifest that had lost
its download pointers, a table truncated by a narrowed re-run and a raw file no
row points at all look exactly like a healthy corpus until something reads
them. Findings, not fixes -- what to do about an orphan is an operator decision.

It is also the dry-run instrument for the migration: the deployed corpus was
measured with no ``data/manifest/`` and no tables at all -- gigabytes of raw
bytes and tens of thousands of period summaries that nothing points at -- and
this command must be able to say that plainly rather than crash on the first
missing directory.

What it costs. Every check is metadata-only by default: the manifests and
tables are read (they are small), and every artefact pointer is resolved
against ONE listing of ``data/raw/`` rather than a stat per pointer, so a
9 GB corpus is a directory walk, not a re-read. ``check_hashes=True`` (the
CLI's ``--hash``) re-reads every stored artefact and is the slow path by
construction -- it is the only check that can catch bytes that rotted under
us, and the only one that cannot be run nightly.

Why no lock. The command opens nothing for writing and creates no directory
(not even the ones ``Config`` names), so it is safe beside a running crawl; it
must therefore never take the corpus lock, or an inspection would block on a
nightly run -- or worse, make an operator kill one to answer a question. A
concurrent writer can make a finding stale between the walk and the read; that
is the price of not blocking, and a re-run settles it.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Config, normalize_cik, normalize_lei
from .eu.documents import Document
from .models import FilingRecord
from .paths import UnsafeIdentifier, safe_component

#: The manifest pointers that name bytes on disk, in the order a reader falls
#: back through them. ``Config.sticky_manifest_fields`` is the same set minus
#: ``sha256`` (which is a hash, not a pointer); it is not reused here because
#: an operator who narrows the sticky set must not thereby narrow what is
#: CHECKED -- the pointers a record carries are a fact of the schema.
ARTEFACT_POINTERS: tuple[str, ...] = (
    "local_path", "primary_path", "text_path", "pdf_path",
)

#: Raw files that legitimately exist with no manifest row pointing at them.
#: ``Storage.store_companyfacts`` keeps the SEC's raw companyfacts JSON as
#: provenance for the F1 summaries derived from it; it is an input, not a
#: document, so it never gets a manifest row. Anything else under ``raw/`` is
#: expected to be reachable from a manifest.
UNREFERENCED_RAW_NAMES: frozenset[str] = frozenset({"companyfacts.json"})

#: How many findings of one kind the text report prints before summarising the
#: rest as a count. The JSON report always carries every finding.
MAX_EXAMPLES = 10

#: Read size for the hashing pass: large enough that a 100 MB submission is a
#: few hundred reads, small enough that a corpus of them never sits in memory.
_HASH_CHUNK = 1 << 20


@dataclass(frozen=True)
class Finding:
    """One thing that is wrong, named so an operator can act on it.

    ``kind`` is the check that fired (stable, greppable, safe to count on);
    ``subject`` is what is wrong (a path relative to the data dir, or a
    ``file:row`` / ``file:doc_id`` locator); ``detail`` is the evidence.
    """

    kind: str
    subject: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CorpusScan:
    """The outcome of one pass: what was looked at, and what was found."""

    data_dir: Path
    findings: list[Finding] = field(default_factory=list)
    scanned: dict[str, int] = field(default_factory=dict)
    #: Facts about the corpus that are not findings about a document: what is
    #: missing WHOLESALE. A corpus with no ``data/manifest/`` produces one
    #: orphan finding per file and no explanation; the note is the explanation.
    notes: list[str] = field(default_factory=list)
    hashed: bool = False
    #: False once anything the reference set is built from could not be read.
    #: The orphan check is then skipped entirely rather than reporting every
    #: artefact of an unreadable manifest as unreferenced -- one unreadable
    #: file would otherwise produce thousands of false findings.
    orphans_checked: bool = True

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.findings:
            counts[f.kind] = counts.get(f.kind, 0) + 1
        return counts


def verify(config: Config, *, ciks: Sequence[str] | None = None,
           check_hashes: bool = False) -> list[Finding]:
    """Return every integrity finding over ``config.data_dir``, in a stable order.

    ``ciks`` narrows the pass to those SEC issuers: their manifests, their
    financials/ownership tables and their ``raw/<cik>/`` subtrees. The EU
    pillar is keyed by LEI, so a CIK filter skips it rather than pretending to
    have checked it.
    """
    return scan(config, ciks=ciks, check_hashes=check_hashes).findings


def scan(config: Config, *, ciks: Sequence[str] | None = None,
         check_hashes: bool = False) -> CorpusScan:
    """:func:`verify` plus the counters the CLI reports ("what was looked at")."""
    return _Scan(config, ciks=ciks, check_hashes=check_hashes).run()


class _Scan:
    """One pass over a corpus. Holds the reference set the orphan check needs."""

    def __init__(self, config: Config, *, ciks: Sequence[str] | None,
                 check_hashes: bool):
        self.config = config
        self.data_dir = config.data_dir
        self.ciks = [normalize_cik(c) for c in ciks] if ciks else None
        self.check_hashes = check_hashes
        self.result = CorpusScan(data_dir=self.data_dir, hashed=check_hashes)
        self._findings: list[Finding] = []
        #: Every regular file under ``raw/``, relative to the data dir -- the
        #: one listing every pointer is resolved against. Under a ``ciks``
        #: filter it covers only those issuers' subtrees, so it can say
        #: "present" but never "absent" (see :meth:`_exists`).
        self._raw: set[str] = set()
        #: rel paths some manifest row points at, whether or not they exist.
        self._referenced: set[str] = set()
        self._counts = {"manifests": 0, "rows": 0, "eu_manifests": 0,
                        "tables": 0, "table_rows": 0, "raw_files": 0}

    # ---- reporting helpers ----
    def _add(self, kind: str, subject, detail: str) -> None:
        self._findings.append(Finding(kind, str(subject), detail))

    def _blind(self) -> None:
        """Part of the reference set could not be read or resolved.

        An unreadable manifest, an unparseable row, a pointer that escapes the
        data directory: in each case we no longer know what that row referred
        to, and "unreferenced" can no longer be told apart from "referenced by
        something we could not read". The orphan check is dropped for the whole
        pass rather than turned into thousands of false positives.
        """
        self.result.orphans_checked = False

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.data_dir))
        except ValueError:
            return str(path)

    # ---- the pass ----
    def run(self) -> CorpusScan:
        self._index_raw()
        self._check_sec_manifests()
        if self.ciks is None:
            self._check_eu_manifests()
        self._check_tables()
        self._check_orphans()
        self._note_what_is_absent()

        seen: dict[Finding, None] = dict.fromkeys(self._findings)
        self.result.findings = sorted(
            seen, key=lambda f: (f.kind, f.subject, f.detail))
        self._counts["raw_files"] = len(self._raw)
        self.result.scanned = self._counts
        return self.result

    def _note_what_is_absent(self) -> None:
        """Say plainly what a corpus is missing WHOLESALE, not file by file.

        The deployed corpus was measured with gigabytes under ``raw/`` and no
        ``data/manifest/`` and no tables at all. Reporting that as N thousand
        individual orphans is true and useless; the operator needs the one
        sentence that explains all N of them.
        """
        manifest_rel = self._rel(self.config.manifest_dir)
        indexed = self._counts["manifests"] or self._counts["eu_manifests"]
        if not indexed:
            missing = ("no " if not self.config.manifest_dir.is_dir()
                       else "an empty ")
            consequence = (
                f", so all {len(self._raw)} file(s) under raw/ are unreachable"
                if self._raw else "")
            self.result.notes.append(
                f"{missing}{manifest_rel}/: nothing indexes this corpus"
                f"{consequence} (re-run discover / xbrl / eu-acquire to rebuild "
                "the index)")
        tables = (self.config.financials_dir, self.config.financials_eu_dir,
                  self.config.financials_register_dir, self.config.ownership_dir)
        if not any(d.is_dir() for d in tables):
            self.result.notes.append(
                "no financials/ownership tables at all: the queryable layer has "
                "never been built here")

    # ---- raw listing ----
    def _index_raw(self) -> None:
        """List ``raw/`` once. Every pointer is then a set membership, not a stat.

        The listing is held in memory (a corpus of a million artefacts costs
        tens of MB of path strings). That is the deliberate trade: the orphan
        check needs the full set anyway, and resolving each pointer with its
        own ``stat`` turned a walk into hundreds of thousands of syscalls
        against a network share.
        """
        roots = [self.config.raw_dir]
        if self.ciks is not None:
            roots = [self.config.raw_dir / cik for cik in self.ciks]
        for root in roots:
            if not root.is_dir():
                continue
            for dirpath, _dirnames, filenames in os.walk(root, onerror=self._walk_error):
                base = Path(dirpath)
                for name in filenames:
                    self._raw.add(self._rel(base / name))

    def _walk_error(self, exc: OSError) -> None:
        where = Path(exc.filename) if exc.filename else self.config.raw_dir
        self._add("unreadable-file", self._rel(where),
                  f"cannot list the directory: {exc.strerror}")
        self._blind()

    def _exists(self, rel: str) -> bool:
        if rel in self._raw:
            return True
        # Not in the listing is only proof of absence where the listing is
        # authoritative: under ``raw/``, and only when the whole of it was
        # walked. A pointer elsewhere (a legacy row, a corpus laid out by hand)
        # or any pointer at all under a ``ciks`` filter -- which walks a few
        # subtrees -- falls back to a stat rather than being called missing on
        # the strength of a listing that never covered it.
        if self.ciks is not None or not rel.startswith("raw" + os.sep):
            return (self.data_dir / rel).exists()
        return False

    # ---- SEC manifests ----
    def _sec_manifest_files(self) -> list[Path]:
        d = self.config.manifest_dir
        if self.ciks is not None:
            return [d / f"{cik}.jsonl" for cik in self.ciks if (d / f"{cik}.jsonl").is_file()]
        if not d.is_dir():
            return []
        return sorted(p for p in d.glob("*.jsonl") if p.is_file())

    def _check_sec_manifests(self) -> None:
        for path in self._sec_manifest_files():
            self._counts["manifests"] += 1
            rel = self._rel(path)
            try:
                cik = normalize_cik(path.stem)
            except ValueError:
                # Named by something that is not a CIK. Its rows are still read
                # (their artefacts are real bytes and must not be reported as
                # orphans), but they cannot be attributed to an issuer.
                self._add("invalid-identifier", rel,
                          f"manifest filename is not a CIK: {path.stem!r}")
                cik = None
            lines = self._read_lines(path)
            if lines is None:
                continue
            self._check_sec_rows(rel, cik, lines)

    def _read_lines(self, path: Path) -> list[str] | None:
        try:
            return path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            self._add("unreadable-file", self._rel(path), f"cannot read it: {exc}")
            self._blind()
            return None

    def _check_sec_rows(self, rel: str, cik: str | None, lines: list[str]) -> None:
        by_doc_id: dict[str, int] = {}
        for lineno, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            self._counts["rows"] += 1
            where = f"{rel}:{lineno}"
            row = self._parse_row(where, line)
            if row is None:
                continue
            row_cik = row.get("cik")
            try:
                row_cik = normalize_cik(row_cik)
            except (ValueError, TypeError):
                self._add("invalid-identifier", where,
                          f"row cik is not a usable CIK: {row.get('cik')!r}")
                self._blind()
                continue
            if cik is not None and row_cik != cik:
                self._add("foreign-row", where,
                          f"row is filed under {row_cik} in {cik}'s manifest")
            try:
                record = FilingRecord.from_row(row)
            except Exception as exc:  # noqa: BLE001 - a manifest row is untrusted
                self._add("unreadable-row", where,
                          f"not a filing record ({type(exc).__name__}: {exc})")
                self._blind()
                continue

            stored_id = str(row.get("doc_id") or "")
            by_doc_id[stored_id] = by_doc_id.get(stored_id, 0) + 1
            if stored_id != record.doc_id:
                self._add(
                    "stale-doc-id", f"{rel}:{stored_id or '(none)'}",
                    "the identity rule does not reproduce this id: "
                    f"{record.form_type.code} {record.accession} is {record.doc_id}")
            self._check_pointers(f"{rel}:{stored_id or lineno}", record)

        for doc_id, count in by_doc_id.items():
            if count > 1:
                self._add("duplicate-doc-id", f"{rel}:{doc_id or '(none)'}",
                          f"{count} rows share this doc_id in one manifest")

    def _parse_row(self, where: str, line: str) -> dict | None:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            self._add("unreadable-row", where, f"not JSON ({exc})")
            self._blind()
            return None
        if not isinstance(row, dict):
            self._add("unreadable-row", where,
                      f"not a JSON object but a {type(row).__name__}")
            self._blind()
            return None
        return row

    def _check_pointers(self, where: str, record: FilingRecord) -> None:
        pointers = {name: getattr(record, name) for name in ARTEFACT_POINTERS}
        for name, rel in pointers.items():
            if not rel:
                continue
            if not self._inside_data_dir(rel):
                self._add("missing-artefact", where,
                          f"{name} points outside the data directory: {rel}")
                self._blind()
                continue
            self._referenced.add(rel)
            if not self._exists(rel):
                self._add("missing-artefact", where, f"{name} is not on disk: {rel}")

        if any(pointers.values()) and not record.sha256:
            # Storage._needs_repair's own marker: a record with bytes on disk
            # and no hash was interrupted between writing and stamping, and is
            # invisible to every consumer that trusts the hash chain.
            self._add("incomplete-record", where,
                      "an artefact is stored but sha256 is unset "
                      "(half-processed; `download` repairs it from disk)")
        elif self.check_hashes and record.sha256:
            self._check_hash(where, record)

    def _check_hash(self, where: str, record: FilingRecord) -> None:
        """Re-hash what ``sha256`` actually covers.

        Which artefact that is depends on the producer, and there is no third
        spelling: ``fetch_and_store`` hashes the complete submission it stored
        (``local_path``), while ``write_financial_summary`` -- family F, which
        has no submission -- hashes the HTML it rendered (``primary_path``).
        """
        rel = record.local_path or record.primary_path
        if not rel or not self._inside_data_dir(rel) or not self._exists(rel):
            return  # already reported as a missing artefact
        actual = self._sha256_file(where, self.data_dir / rel)
        if actual is not None and actual != record.sha256:
            self._add("hash-mismatch", where,
                      f"stored sha256 {record.sha256[:12]}… but {rel} hashes "
                      f"to {actual[:12]}…")

    def _inside_data_dir(self, rel: str) -> bool:
        candidate = Path(rel)
        if candidate.is_absolute():
            return False
        return ".." not in candidate.parts

    def _sha256_file(self, where: str, path: Path) -> str | None:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as fh:
                while chunk := fh.read(_HASH_CHUNK):
                    digest.update(chunk)
        except OSError as exc:
            self._add("unreadable-file", self._rel(path), f"cannot read it: {exc}")
            return None
        return digest.hexdigest()

    # ---- EU manifests ----
    def _check_eu_manifests(self) -> None:
        root = self.config.manifest_dir
        if not root.is_dir():
            return
        seen: dict[str, str] = {}
        for lei_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            try:
                lei = normalize_lei(lei_dir.name)
            except ValueError:
                self._add("invalid-identifier", self._rel(lei_dir),
                          f"manifest directory is not a LEI: {lei_dir.name!r}")
                lei = None
            for path in sorted(lei_dir.glob("*.json")):
                self._counts["eu_manifests"] += 1
                self._check_eu_manifest(path, lei, seen)

    def _check_eu_manifest(self, path: Path, lei: str | None,
                           seen: dict[str, str]) -> None:
        rel = self._rel(path)
        # ``unreadable-file``, not ``unreadable-row``: an EU manifest is ONE JSON
        # document per file, so a parse failure loses the whole document's
        # provenance (remedy: re-acquire it), where an unreadable row is one bad
        # line in a JSONL file whose other lines are still good.
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._add("unreadable-file", rel, f"not a readable manifest ({exc})")
            self._blind()
            return
        if not isinstance(manifest, dict):
            self._add("unreadable-file", rel,
                      f"not a JSON object but a {type(manifest).__name__}")
            self._blind()
            return

        doc_id = str(manifest.get("doc_id") or path.stem)
        if doc_id != path.stem:
            self._add("foreign-row", rel,
                      f"manifest says doc_id {doc_id} but is filed as {path.stem}")
        if lei is not None and str(manifest.get("lei") or "") != lei:
            self._add("foreign-row", rel,
                      f"manifest lei is {manifest.get('lei')!r} under {lei}/")
        if doc_id in seen and seen[doc_id] != rel:
            self._add("duplicate-doc-id", doc_id,
                      f"one doc_id under two manifests: {seen[doc_id]} and {rel}")
        seen.setdefault(doc_id, rel)

        self._check_eu_identity(rel, doc_id, manifest)
        self._check_eu_files(rel, manifest)

    def _check_eu_identity(self, rel: str, doc_id: str, manifest: dict) -> None:
        """Recompute ``sha1(source|country|native_id)`` and compare.

        A manifest written before Task 8 carries no ``native_id`` at all, so
        its id can never be recomputed -- which is itself the finding: those
        documents are unreachable under the current scheme and only a re-run of
        ``eu-acquire`` settles them.
        """
        native_id = manifest.get("native_id")
        if not native_id or not str(native_id).strip():
            self._add("stale-doc-id", rel,
                      "no native_id recorded: this manifest predates the "
                      "computed EU identity and its doc_id cannot be checked")
            return
        try:
            expected = Document(
                native_id=str(native_id), lei=manifest.get("lei"),
                country=str(manifest.get("country") or ""),
                doc_type=str(manifest.get("doc_type") or "other"),
                period_end=None, published_ts=None, discovered_ts="",
                language=None, source=str(manifest.get("source") or ""),
            ).doc_id
        except ValueError as exc:
            self._add("stale-doc-id", rel, f"identity cannot be recomputed: {exc}")
            return
        if expected != doc_id:
            self._add("stale-doc-id", rel,
                      f"the identity rule computes {expected} for "
                      f"{manifest.get('source')}|{manifest.get('country')}|{native_id}")

    def _check_eu_files(self, rel: str, manifest: dict) -> None:
        files = manifest.get("files")
        if not isinstance(files, list):
            return
        for entry in files:
            if not isinstance(entry, dict):
                continue
            path_rel = entry.get("path")
            if not path_rel:
                continue  # an index-only file: recorded, deliberately not fetched
            path_rel = str(path_rel)
            if not self._inside_data_dir(path_rel):
                self._add("missing-artefact", rel,
                          f"file points outside the data directory: {path_rel}")
                self._blind()
                continue
            self._referenced.add(path_rel)
            if not self._exists(path_rel):
                self._add("missing-artefact", rel, f"file is not on disk: {path_rel}")
                continue
            stored = entry.get("sha256")
            if self.check_hashes and stored:
                actual = self._sha256_file(rel, self.data_dir / path_rel)
                if actual is not None and actual != stored:
                    self._add("hash-mismatch", rel,
                              f"stored sha256 {str(stored)[:12]}… but {path_rel} "
                              f"hashes to {actual[:12]}…")

    # ---- tables ----
    def _check_tables(self) -> None:
        """Every per-entity table: is each row filed under the entity naming its file?

        A table is named by its entity and holds only that entity's rows; a row
        carrying another entity's id means a writer keyed a path off one
        identifier and the row off another, and the entity it claims to
        describe has that data nowhere.
        """
        specs = (
            (self.config.financials_dir, "entity_id", _as_cik),
            (self.config.financials_eu_dir, "entity_id", _as_lei),
            (self.config.financials_register_dir, "entity_id", _as_component),
            (self.config.ownership_dir, "cik", _as_cik),
        )
        for directory, id_field, normalizer in specs:
            for path in self._table_files(directory, normalizer):
                self._counts["tables"] += 1
                lines = self._read_lines(path)
                if lines is None:
                    continue
                self._check_table_rows(path, id_field, normalizer, lines)

    def _table_files(self, directory: Path, normalizer) -> list[Path]:
        if not directory.is_dir():
            return []
        if self.ciks is not None and normalizer is _as_cik:
            return [directory / f"{cik}.jsonl" for cik in self.ciks
                    if (directory / f"{cik}.jsonl").is_file()]
        if self.ciks is not None:
            return []
        return sorted(p for p in directory.glob("*.jsonl") if p.is_file())

    def _check_table_rows(self, path: Path, id_field: str, normalizer,
                          lines: list[str]) -> None:
        rel = self._rel(path)
        expected = normalizer(path.stem, self.config)
        if expected is None:
            self._add("invalid-identifier", rel,
                      f"table filename is not a usable entity id: {path.stem!r}")
            return
        for lineno, line in enumerate(lines, 1):
            line = line.strip()
            if not line:
                continue
            self._counts["table_rows"] += 1
            row = self._parse_row(f"{rel}:{lineno}", line)
            if row is None:
                continue
            value = row.get(id_field)
            if value is None:
                continue  # a table whose rows carry no entity id: nothing to compare
            if normalizer(value, self.config) != expected:
                self._add("foreign-row", f"{rel}:{lineno}",
                          f"{id_field} is {value!r} in {expected}'s table")

    # ---- orphans ----
    def _check_orphans(self) -> None:
        if not self.result.orphans_checked:
            return
        for rel in self._raw:
            if rel in self._referenced:
                continue
            name = Path(rel).name
            if name in UNREFERENCED_RAW_NAMES:
                continue
            self._add("orphan-artefact", rel, _orphan_detail(name))


#: Suffixes both atomic writers stage through before ``os.replace``:
#: ``_atomic_write_text`` uses ``.tmp``, the EU downloader ``.part``. A file
#: still carrying one is the residue of an interrupted write, not a document
#: the index lost.
STAGING_SUFFIXES: frozenset[str] = frozenset({".tmp", ".part"})


def _orphan_detail(name: str) -> str:
    """Why this file is unreferenced, in the terms an operator has to act on.

    A leftover staging file reported as a bare orphan reads exactly like a
    document whose manifest row was destroyed -- which is the one finding that
    must be investigated before anything is deleted. Naming it removes the
    investigation: nothing ever pointed at it, and nothing ever will.
    """
    if Path(name).suffix in STAGING_SUFFIXES:
        return "interrupted atomic write, safe to delete"
    return "no manifest row points at this file"


# ---- identifier normalisers, one per table family ----
def _as_cik(value, config: Config) -> str | None:
    try:
        return normalize_cik(value)
    except (ValueError, TypeError):
        return None


def _as_lei(value, config: Config) -> str | None:
    try:
        return normalize_lei(value)
    except (ValueError, TypeError):
        return None


def _as_component(value, config: Config) -> str | None:
    try:
        return safe_component(value, max_length=config.max_path_component_length)
    except UnsafeIdentifier:
        return None


# ---- rendering ----
def as_json(result: CorpusScan) -> dict:
    """The machine-readable report: findings plus what was looked at."""
    return {
        "data_dir": str(result.data_dir),
        "hashed": result.hashed,
        "orphans_checked": result.orphans_checked,
        "scanned": result.scanned,
        "notes": result.notes,
        "counts": result.counts,
        "findings": [f.to_dict() for f in result.findings],
    }


def format_text(result: CorpusScan) -> str:
    """The human report: a summary, then up to :data:`MAX_EXAMPLES` per kind."""
    s = result.scanned
    lines = [
        f"verify {result.data_dir} (read-only: nothing is written, "
        f"no corpus lock is taken)",
        f"  scanned: {s.get('manifests', 0)} manifest(s) / {s.get('rows', 0)} row(s), "
        f"{s.get('eu_manifests', 0)} EU manifest(s), {s.get('tables', 0)} table(s) / "
        f"{s.get('table_rows', 0)} row(s), {s.get('raw_files', 0)} raw file(s)"
        + ("" if result.hashed else "  [no hashing: pass --hash]"),
        f"  findings: {len(result.findings)}",
    ]
    lines.extend(f"  note: {note}" for note in result.notes)
    if not result.orphans_checked:
        lines.append("  orphan check SKIPPED: part of the manifest could not be "
                     "read, so 'unreferenced' cannot be told from 'unknown'")
    for kind, count in sorted(result.counts.items()):
        lines.append(f"  {kind}: {count}")
        examples = [f for f in result.findings if f.kind == kind]
        for finding in examples[:MAX_EXAMPLES]:
            lines.append(f"    {finding.subject}: {finding.detail}")
        if count > MAX_EXAMPLES:
            lines.append(f"    … and {count - MAX_EXAMPLES} more (use --json for all)")
    return "\n".join(lines)
