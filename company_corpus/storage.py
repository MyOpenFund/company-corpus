"""Manifest storage, deduplication, and the discovery-error audit trail.

Parallels ``cb_corpus.storage`` (manifest portion). Records are kept in
per-issuer JSONL at ``data/manifest/<cik>.jsonl``, keyed by the stable
``doc_id``. Saving is idempotent: an existing ``doc_id`` is updated in place
(metadata corrections) rather than duplicated. Raw-file download lands in
Phase 2; this module owns the metadata layer.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .config import Config, normalize_cik
from .extract import clean_text
from .models import FilingRecord
from .submission import filename_from_url, parse_submission, select_primary


def _read_umask() -> int:
    """The process umask, read once at import.

    ``os.umask`` is a set-and-return call with no getter, so the only way to read
    the mask is to set it and put it back. That two-step is not atomic, so it is
    done here at import time -- before this process has spawned any thread that
    could create a file while the mask is momentarily 0o077.
    """
    value = os.umask(0o077)
    os.umask(value)
    return value


_UMASK = _read_umask()


def data_file_mode() -> int:
    """The permissions a corpus file must end up with.

    ``tempfile.mkstemp`` hardcodes 0o600 (it is built for secrets), so an atomic
    write through it produced manifests, tables, extracts and raw downloads that
    only the crawling account could read -- the RAG ingester and the NAS share
    consumers run as other accounts. Reapply what a plain ``open()`` would have
    given: 0o666 masked by the umask, i.e. the operator's own policy.
    """
    return 0o666 & ~_UMASK


def _atomic_write_text(path: Path, data: str) -> None:
    """Write ``data`` to ``path`` atomically: a UNIQUE tmp sibling + ``os.replace``.

    An interrupt mid-write leaves a tmp file behind, never a truncated
    destination. The tmp name is unique per call: a fixed ``<name>.tmp`` sibling
    meant two writers of the same destination shared one temp path, so whichever
    called ``os.replace`` second died with FileNotFoundError and lost its whole
    write (DI-I1 / Rob-I9).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            # Undo mkstemp's 0o600 before the rename, so the destination is never
            # visible under its final name with owner-only permissions.
            os.fchmod(fd, data_file_mode())
        os.replace(tmp, path)  # atomic on the same filesystem (tmp is a sibling)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _jsonl(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


@dataclass
class SaveStats:
    """Outcome of merging a batch of records into a manifest."""

    seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0

    def __iadd__(self, other: "SaveStats") -> "SaveStats":
        self.seen += other.seen
        self.added += other.added
        self.updated += other.updated
        self.unchanged += other.unchanged
        return self


@dataclass
class DownloadResult:
    """Outcome of fetching + decomposing a single filing."""

    doc_id: str
    # downloaded | repaired | skipped | would-download | would-repair | empty | error
    status: str
    bytes: int = 0
    error: str | None = None


@dataclass
class RenderResult:
    """Outcome of rendering a single filing's primary document to PDF."""

    doc_id: str
    status: str  # rendered | skipped | would-render | no-primary | error
    error: str | None = None


class Storage:
    """Read/write per-issuer manifests and append discovery errors."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()

    # ---- manifests ----
    def load_manifest(self, cik: str) -> dict[str, FilingRecord]:
        """Return ``{doc_id: FilingRecord}`` for an issuer (empty if none)."""
        path = self.config.manifest_file(cik)
        records: dict[str, FilingRecord] = {}
        if not path.exists():
            return records
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = FilingRecord.from_row(json.loads(line))
            except (json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
                # One corrupt line (e.g. from an interrupted legacy write) must not
                # take down the whole issuer's manifest -- skip it and warn.
                warnings.warn(f"{path}:{lineno}: skipping unparseable manifest row ({exc})",
                              stacklevel=2)
                continue
            records[rec.doc_id] = rec
        return records

    def save_records(
        self, records: Iterable[FilingRecord], *, dry_run: bool = True
    ) -> SaveStats:
        """Merge ``records`` into their per-issuer manifests.

        With ``dry_run=True`` (default) nothing is written; the returned stats
        still reflect what *would* change. Records are grouped by CIK so a batch
        spanning issuers is handled in one call.
        """
        by_cik: dict[str, list[FilingRecord]] = {}
        for rec in records:
            by_cik.setdefault(normalize_cik(rec.cik), []).append(rec)

        total = SaveStats()
        for cik, recs in by_cik.items():
            total += self._save_cik(cik, recs, dry_run=dry_run)
        return total

    def _carry_sticky(self, prior: FilingRecord, rec: FilingRecord) -> None:
        """Fill the incoming record's empty artefact pointers from the stored one.

        ``EdgarSubmissions.discover`` builds a fresh record on every run with
        ``local_path``/``sha256``/``primary_path``/``text_path``/``pdf_path``
        unset -- those are only ever written by ``fetch_and_store`` and
        ``render_record``. The merge used to REPLACE the stored record with that
        fresh one, so a second ``discover --write`` orphaned every downloaded
        byte and destroyed the corpus's hash chain (DI-C1). Only *empty*
        incoming fields are filled, so a run that genuinely re-derives an
        artefact still wins.
        """
        for name in self.config.sticky_manifest_fields:
            if not getattr(rec, name, None) and getattr(prior, name, None):
                setattr(rec, name, getattr(prior, name))

    def _save_cik(
        self, cik: str, records: list[FilingRecord], *, dry_run: bool
    ) -> SaveStats:
        existing = self.load_manifest(cik)
        stats = SaveStats(seen=len(records))
        changed = False
        for rec in records:
            prior = existing.get(rec.doc_id)
            if prior is None:
                existing[rec.doc_id] = rec
                stats.added += 1
                changed = True
            elif prior.to_row() != rec.to_row():
                self._carry_sticky(prior, rec)
                if prior.to_row() != rec.to_row():
                    existing[rec.doc_id] = rec
                    stats.updated += 1
                    changed = True
                else:
                    # Nothing but the pointers differed: not an update.
                    stats.unchanged += 1
            else:
                stats.unchanged += 1

        if changed and not dry_run:
            self._write_manifest(cik, existing.values())
        return stats

    def _write_manifest(self, cik: str, records: Iterable[FilingRecord]) -> None:
        path = self.config.manifest_file(cik)
        # Deterministic order: by filing date then accession, for stable diffs.
        ordered = sorted(
            records,
            key=lambda r: (r.filing_date or date.min, r.accession),
        )
        _atomic_write_text(path, _jsonl(rec.to_row() for rec in ordered))

    # ---- download + decomposition (Phase 2) ----
    def raw_dir_for(self, record: FilingRecord) -> Path:
        year = str(record.year) if record.year else "unknown"
        return self.config.raw_dir / record.cik / record.form_type.code / year

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.config.data_dir))

    def _needs_repair(self, record: FilingRecord) -> bool:
        """Is a stored submission only half-processed?

        The skip test used to be ``sub_path.exists()`` alone, so an interrupt
        (SIGTERM, OOM, disk-full) between writing the submission and saving the
        manifest left the document permanently stuck: no primary, no cleaned
        text, no hash, invisible to ``rag.iter_items`` forever, recoverable only
        by re-downloading every byte with ``--overwrite`` (Rob-C4). A missing
        ``sha256`` is the marker: it is set by every complete pass, and it is
        also what a wiped record (DI-C1) or a legacy row lacks (DI-M10). A
        pointer aimed at bytes that are no longer on disk counts too -- since
        DI-C1 made the pointers sticky, a dangling one can never clear itself.
        """
        if not record.sha256:
            return True
        for rel in (record.local_path, record.primary_path, record.text_path):
            if rel and not (self.config.data_dir / rel).exists():
                return True
        return False

    def _decompose(self, record: FilingRecord, raw: str, dest_dir: Path) -> None:
        """Hash the submission and write the primary + cleaned-text artefacts.

        Shared by the download and the repair paths, and -- unlike the inline
        version it replaces -- always called from INSIDE the caller's ``try``:
        one malformed submission used to abort the whole nightly download run
        mid-issuer, which is precisely how a half-processed document is created
        (Rob-C4).

        The record is mutated only once every byte is parsed and written. The
        old inline version stamped ``sha256`` before parsing, so a parse that
        blew up left a record that ``_needs_repair`` would call complete --
        stuck forever, because DI-C1 made the pointers sticky and a later save
        can no longer clear them.
        """
        if not raw.strip():
            # A truncated or interrupted transfer. Adopting it would stamp the
            # hash of nothing on the record and declare the document done.
            raise ValueError("stored submission is empty")

        sha256 = hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()
        primary = select_primary(
            parse_submission(raw),
            primary_filename=filename_from_url(record.primary_doc_url),
            sec_form=record.sec_form,
        )
        primary_rel = text_rel = None
        if primary and primary.text:
            ext = Path(primary.filename).suffix or ".txt"
            primary_path = dest_dir / f"{record.doc_id}.primary{ext}"
            _atomic_write_text(primary_path, primary.text)
            primary_rel = self._rel(primary_path)

            text_path = dest_dir / f"{record.doc_id}.txt"
            _atomic_write_text(text_path, clean_text(primary.text, primary.filename))
            text_rel = self._rel(text_path)

        record.sha256 = sha256
        if primary_rel:
            record.primary_path = primary_rel
            record.text_path = text_rel

    def fetch_and_store(
        self,
        record: FilingRecord,
        fetcher,
        *,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> DownloadResult:
        """Download a filing's complete submission and decompose it.

        Writes three layered artifacts under ``data/raw/<cik>/<form>/<year>/``:
        the full submission (``.submission.txt``), the decomposed primary
        document (``.primary<ext>``), and cleaned text (``.txt``). Mutates
        ``record`` with the resulting paths + sha256.

        Idempotent *and* convergent: an existing submission is skipped only when
        the derived artefacts are actually there, otherwise it is repaired from
        the bytes already on disk -- no network, no ``--overwrite`` (Rob-C4).
        """
        dest_dir = self.raw_dir_for(record)
        sub_path = dest_dir / f"{record.doc_id}.submission.txt"

        if sub_path.exists() and not overwrite:
            record.local_path = self._rel(sub_path)
            if not self._needs_repair(record):
                return DownloadResult(record.doc_id, "skipped")
            if dry_run:
                return DownloadResult(record.doc_id, "would-repair")
            try:
                raw = sub_path.read_text(encoding="utf-8")
                self._decompose(record, raw, dest_dir)
            except Exception as exc:  # noqa: BLE001
                return DownloadResult(record.doc_id, "error",
                                      error=f"repairing stored submission: {exc}")
            return DownloadResult(record.doc_id, "repaired")

        if dry_run:
            return DownloadResult(record.doc_id, "would-download")
        if not record.submission_url:
            return DownloadResult(record.doc_id, "error", error="no submission_url")

        try:
            raw = fetcher.get_text(record.submission_url)
            data = raw.encode("utf-8", "replace")
            _atomic_write_text(sub_path, raw)
            record.local_path = self._rel(sub_path)
            self._decompose(record, raw, dest_dir)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(record.doc_id, "error", error=str(exc))

        return DownloadResult(record.doc_id, "downloaded", bytes=len(data))

    # ---- PDF rendering (Phase 3, separate batch) ----
    def render_record(
        self,
        record: FilingRecord,
        renderer,
        *,
        dry_run: bool = False,
        overwrite: bool = False,
    ) -> RenderResult:
        """Render a filing's primary document to PDF via ``renderer``.

        ``renderer`` is a ``Callable[[Path, Path], None]`` (see
        :func:`company_corpus.render.make_chrome_renderer`). Requires the
        primary document to have been downloaded (Phase 2). Mutates ``record``
        with ``pdf_path``. Idempotent: an existing PDF is skipped unless
        ``overwrite``.
        """
        if not record.primary_path:
            return RenderResult(record.doc_id, "no-primary")

        src = self.config.data_dir / record.primary_path
        if not src.exists():
            return RenderResult(record.doc_id, "no-primary",
                                error=f"primary not on disk: {record.primary_path}")

        pdf_path = self.raw_dir_for(record) / f"{record.doc_id}.pdf"
        if pdf_path.exists() and not overwrite:
            record.pdf_path = self._rel(pdf_path)
            return RenderResult(record.doc_id, "skipped")
        if dry_run:
            return RenderResult(record.doc_id, "would-render")

        try:
            renderer(src, pdf_path)
        except Exception as exc:  # noqa: BLE001
            return RenderResult(record.doc_id, "error", error=str(exc))

        record.pdf_path = self._rel(pdf_path)
        return RenderResult(record.doc_id, "rendered")

    # ---- XBRL financials (Phase 4) ----
    def store_companyfacts(self, cik: str, facts: dict) -> tuple[str, str]:
        """Write the raw company-facts JSON once per issuer. Returns (rel_path, sha256)."""
        cik = normalize_cik(cik)
        path = self.config.raw_dir / cik / "F1" / "companyfacts.json"
        blob = json.dumps(facts, ensure_ascii=False)
        _atomic_write_text(path, blob)
        return self._rel(path), hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _write_financials_table(
        self, ident: str, rows: Iterable[dict], *, subdir: Path
    ) -> str:
        """Atomically write a per-entity financials JSONL table ``<subdir>/<ident>.jsonl``.

        Shared core for the SEC/EU/register writers below: they differ only by the
        target subdirectory (and how ``ident`` is normalized by the caller).
        """
        path = subdir / f"{ident}.jsonl"
        _atomic_write_text(path, _jsonl(rows))
        return self._rel(path)

    def write_financials_table(self, cik: str, rows: Iterable[dict]) -> str:
        """Write the normalized, queryable facts table data/financials/<cik>.jsonl."""
        return self._write_financials_table(
            normalize_cik(cik), rows, subdir=self.config.financials_dir
        )

    def write_eu_financials_table(self, lei: str, rows: Iterable[dict]) -> str:
        """Write the normalized EU IFRS facts table data/financials_eu/<lei>.jsonl."""
        return self._write_financials_table(
            lei, rows, subdir=self.config.financials_eu_dir
        )

    def write_register_financials_table(self, entity_id: str, rows: Iterable[dict]) -> str:
        """Write the register financials table data/financials_register/<entity_id>.jsonl."""
        return self._write_financials_table(
            entity_id, rows, subdir=self.config.financials_register_dir
        )

    def write_financial_summary(self, record: FilingRecord, html: str, text: str) -> None:
        """Write a period summary's HTML (primary) + clean text; mutate record paths."""
        dest_dir = self.raw_dir_for(record)
        primary = dest_dir / f"{record.doc_id}.primary.html"
        _atomic_write_text(primary, html)
        record.primary_path = self._rel(primary)
        record.sha256 = hashlib.sha256(html.encode("utf-8")).hexdigest()
        txt = dest_dir / f"{record.doc_id}.txt"
        _atomic_write_text(txt, text)
        record.text_path = self._rel(txt)

    # ---- ownership summaries (Phase 4b) ----
    def write_ownership_summary(self, record: FilingRecord, html: str, text: str) -> None:
        """Write a structured ownership summary (HTML primary + clean text)."""
        dest_dir = self.raw_dir_for(record)
        primary = dest_dir / f"{record.doc_id}.primary.html"
        _atomic_write_text(primary, html)
        record.primary_path = self._rel(primary)
        txt = dest_dir / f"{record.doc_id}.txt"
        _atomic_write_text(txt, text)
        record.text_path = self._rel(txt)

    def write_ownership_table(self, cik: str, rows: Iterable[dict]) -> str:
        """Write the normalized ownership rows data/ownership/<cik>.jsonl."""
        cik = normalize_cik(cik)
        path = self.config.ownership_dir / f"{cik}.jsonl"
        _atomic_write_text(path, _jsonl(rows))
        return self._rel(path)

    # ---- discovery errors ----
    def record_errors(self, errors: Iterable[dict], *, run_id: str | None = None) -> int:
        """Append discovery errors to the audit trail. Returns count written.

        Every row is stamped with ``ts`` (ISO-8601 UTC) and, when the caller
        knows it, the ``run_id`` of the run that hit the error — so a dead source
        in the trail can be tied back to its run report in ``runs.jsonl``. Rows
        that already carry either key keep their own value; nothing else is
        rewritten.
        """
        errors = list(errors)
        if not errors:
            return 0
        path = self.config.discovery_errors_path
        path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as fh:
            for err in errors:
                row = dict(err)
                row.setdefault("ts", now)
                if run_id is not None:
                    row.setdefault("run_id", run_id)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return len(errors)
