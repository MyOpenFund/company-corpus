"""Layer-0 archive of the EDGAR submissions API (chantier 8, spec §5).

``raw/<cik>/submissions/`` keeps ``CIK##########.json`` and every history page it lists in
``filings.files``, each next to a ``<file>.meta.json`` sidecar ``{fetched_at, url, sha256}``.
Like ``F1/companyfacts.json`` it is an input, not a document: no manifest row points at it and
``verify`` knows. Every file is fetched before anything is written, and the main file is
written last, so a failed fetch leaves the previous archive intact.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import Config, normalize_cik
from ..paths import safe_component
from ..sources.edgar_submissions import EXTRA_URL, SUBMISSIONS_URL
from ..storage import _atomic_write_text

SUBMISSIONS_DIR = "submissions"
META_SUFFIX = ".meta.json"


class ArchiveMissing(LookupError):
    """No submissions archive on disk for this issuer."""


class ArchiveCorrupt(ValueError):
    """An archive file is missing, unreadable, or does not match its sidecar."""


@dataclass(frozen=True)
class ArchivedFile:
    name: str
    rel_path: str  # relative to the data dir
    sha256: str
    url: str
    fetched_at: datetime


@dataclass
class SubmissionsArchive:
    cik: str
    main: dict
    pages: list[dict]
    files: list[ArchivedFile]  # main first, then the pages in filings.files order

    @property
    def fetched_at(self) -> datetime:
        return self.files[0].fetched_at


def archive_dir(config: Config, cik: str) -> Path:
    return config.raw_dir / normalize_cik(cik) / SUBMISSIONS_DIR


def main_name(cik: str) -> str:
    return f"CIK{normalize_cik(cik)}.json"


def _page_names(main: dict, config: Config) -> list[str]:
    files = (main.get("filings") or {}).get("files") or []
    return [safe_component(f.get("name") if isinstance(f, dict) else None,
                           max_length=config.max_path_component_length) for f in files]


def fetch_archive(config: Config, cik: str, fetcher, *,
                  now: datetime | None = None) -> SubmissionsArchive:
    """Fetch the main file and every history page, then write them with their sidecars."""
    cik = normalize_cik(cik)
    fetched_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    url = SUBMISSIONS_URL.format(cik=cik)
    main_text = fetcher.get_text(url)
    main = json.loads(main_text)
    pages = []
    for name in _page_names(main, config):
        page_url = EXTRA_URL.format(name=name)
        pages.append((name, page_url, fetcher.get_text(page_url)))
    d = archive_dir(config, cik)
    for name, file_url, text in [*pages, (main_name(cik), url, main_text)]:
        _write(d / name, text, file_url, fetched_at)
    return load_archive(config, cik)


def _write(path: Path, text: str, url: str, fetched_at: datetime) -> None:
    _atomic_write_text(path, text)
    meta = {"fetched_at": fetched_at.isoformat(), "url": url,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}
    _atomic_write_text(path.with_name(path.name + META_SUFFIX),
                       json.dumps(meta, sort_keys=True) + "\n")


def load_archive(config: Config, cik: str) -> SubmissionsArchive:
    """Read and verify the archive (every file against its sidecar sha256)."""
    cik = normalize_cik(cik)
    d = archive_dir(config, cik)
    if not (d / main_name(cik)).is_file():
        raise ArchiveMissing(f"no submissions archive for {cik}")
    main_file, main = _read(config, d / main_name(cik))
    try:
        names = _page_names(main, config)
    except ValueError as exc:
        raise ArchiveCorrupt(f"{main_file.rel_path}: unusable page name: {exc}") from exc
    files, pages = [main_file], []
    for name in names:
        f, body = _read(config, d / name)
        files.append(f)
        pages.append(body)
    return SubmissionsArchive(cik=cik, main=main, pages=pages, files=files)


def _read(config: Config, path: Path) -> tuple[ArchivedFile, dict]:
    rel = str(path.relative_to(config.data_dir))
    try:
        raw = path.read_bytes()
        meta = json.loads(path.with_name(path.name + META_SUFFIX).read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(meta["fetched_at"])
        expected, url = meta["sha256"], meta["url"]
        body = json.loads(raw)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ArchiveCorrupt(f"{rel}: {type(exc).__name__}: {exc}") from exc
    actual = hashlib.sha256(raw).hexdigest()
    if actual != expected:
        raise ArchiveCorrupt(f"{rel}: sha256 {actual} does not match its sidecar {expected}")
    if fetched_at.tzinfo is None or not isinstance(body, dict):
        raise ArchiveCorrupt(f"{rel}: naive fetched_at or not a JSON object")
    return ArchivedFile(path.name, rel, actual, url, fetched_at.astimezone(timezone.utc)), body


def acceptance_times(archive: SubmissionsArchive) -> dict[str, datetime]:
    """accession -> EDGAR acceptance time (UTC). A value without a zone is skipped: the API
    writes UTC with a ``Z`` (checked 2026-11-02 against an SGML header: 06:20:51 ET =
    11:20:51Z), so a zone-less value is malformed and never guessed."""
    out: dict[str, datetime] = {}
    blocks = [((archive.main.get("filings") or {}).get("recent") or {}), *archive.pages]
    for block in blocks:
        for accn, when in zip(block.get("accessionNumber") or [],
                              block.get("acceptanceDateTime") or []):
            if not accn or not isinstance(when, str):
                continue
            try:
                t = datetime.fromisoformat(when)
            except ValueError:
                continue
            if t.tzinfo is not None:
                out[accn] = t.astimezone(timezone.utc)
    return out
