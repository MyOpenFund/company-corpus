"""SEC Financial Statement Data Sets -> which tag is on which statement of a filing (spec §5).

Layer 0: ``raw/_sec_statements/<quarter>.zip`` and ``manifest.json``, with hash and fetch date, as
published. ``raw/<cik>/statements/statements.jsonl`` holds one issuer's rows, rebuilt whole and
deterministically from the archives. Like companyfacts these are inputs: no manifest row points at
them and ``verify`` knows. A quarter is either complete on disk or absent: a zip is checked before
it takes its name, and nothing is extracted while an expected quarter is missing.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from ..config import Config, normalize_cik
from ..storage import _atomic_write_text
from .fundamentals import SOURCE_CODE, FundamentalsResult, KnownAtClock, source_doc_id

ARCHIVE_DIR = "_sec_statements"
MANIFEST = "manifest.json"
STATEMENTS_DIR = "statements"
STATEMENTS_FILE = "statements.jsonl"
BULK_URL = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/{quarter}.zip"
FIRST_QUARTER = (2009, 1)
#: The primary statements kept. Equity (EQ), comprehensive income (CI) and the rest are not used by
#: any dictionary concept, and a parenthetical row (``inpth`` = 1) is not the statement itself.
KEPT_STATEMENTS = ("BS", "IS", "CF")
_SUB_COLUMNS = ("adsh", "cik", "form", "period", "filed", "accepted")
_PRE_COLUMNS = ("adsh", "report", "line", "stmt", "inpth", "tag", "version", "plabel")
_CHUNK = 1024 * 1024


class StatementsSourceError(RuntimeError):
    """A bulk file is unreadable or does not have the expected shape."""


@dataclass
class StatementsReport:
    write: bool
    present: int = 0
    fetched: int = 0
    to_fetch: list[str] = field(default_factory=list)
    not_published_yet: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    issuers: int = 0
    rows: int = 0


def archive_root(config: Config) -> Path:
    return config.raw_dir / ARCHIVE_DIR


def statements_path(config: Config, cik: str) -> Path:
    return config.raw_dir / normalize_cik(cik) / STATEMENTS_DIR / STATEMENTS_FILE


def _quarter_end(year: int, q: int) -> date:
    first_of_next = date(year + (q == 4), 1 if q == 4 else 3 * q + 1, 1)
    return first_of_next - timedelta(days=1)


def ended_quarters(today: date, grace_days: int) -> tuple[list[str], list[str]]:
    """``(expected, within_grace)``: quarters that must exist, and quarters ended too recently for
    their absence to mean anything."""
    expected, grace = [], []
    year, q = FIRST_QUARTER
    while _quarter_end(year, q) < today:
        name = f"{year}q{q}"
        (grace if _quarter_end(year, q) + timedelta(days=grace_days) >= today else expected).append(name)
        year, q = (year + 1, 1) if q == 4 else (year, q + 1)
    return expected, grace


def _check_zip(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as zf:
            missing = {"sub.txt", "pre.txt"} - set(zf.namelist())
            if missing:
                raise StatementsSourceError(f"{path.name}: {sorted(missing)} missing")
            if zf.testzip() is not None:
                raise StatementsSourceError(f"{path.name}: corrupt member")
    except (zipfile.BadZipFile, OSError, EOFError) as exc:
        raise StatementsSourceError(f"{path.name}: unreadable zip: {exc}") from exc


def _load_manifest(config: Config) -> dict:
    path = archive_root(config) / MANIFEST
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def _fetch_quarter(config: Config, fetcher, quarter: str, now: datetime) -> bool:
    """Download one quarter; False when the SEC does not have it (404)."""
    root = archive_root(config)
    root.mkdir(parents=True, exist_ok=True)
    url, part = BULK_URL.format(quarter=quarter), root / f"{quarter}.zip.part"
    try:
        resp = fetcher.get(url, stream=True)
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 404:
            return False
        raise
    digest = hashlib.sha256()
    with part.open("wb") as fh:
        for chunk in resp.iter_content(_CHUNK):
            digest.update(chunk)
            fh.write(chunk)
    try:
        _check_zip(part)
    except StatementsSourceError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, root / f"{quarter}.zip")
    manifest = _load_manifest(config)
    manifest[quarter] = {"sha256": digest.hexdigest(), "fetched_at": now.isoformat(), "url": url}
    _atomic_write_text(root / MANIFEST, json.dumps(manifest, sort_keys=True, indent=1) + "\n")
    return True


def _rows(zf: zipfile.ZipFile, name: str, columns: tuple[str, ...]) -> Iterable[dict]:
    """Rows of a tab-separated member, without quoting, as the SEC publishes them."""
    with zf.open(name) as fh:
        text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        header = text.readline().rstrip("\r\n").split("\t")
        missing = [c for c in columns if c not in header]
        if missing:
            raise StatementsSourceError(f"{name}: columns {missing} missing")
        at = {c: header.index(c) for c in columns}
        width = max(at.values())
        for line in text:
            f = line.rstrip("\r\n").split("\t")
            if len(f) > width:
                yield {c: f[i] for c, i in at.items()}


def _read_quarter(path: Path, wanted: Mapping[str, str]) -> dict[str, list[dict]]:
    """``{cik: rows}`` of one archive for the wanted issuers (key = cik without leading zeros)."""
    out: dict[str, list[dict]] = defaultdict(list)
    try:
        with zipfile.ZipFile(path) as zf:
            filings = {r["adsh"]: r for r in _rows(zf, "sub.txt", _SUB_COLUMNS) if r["cik"] in wanted}
            for r in _rows(zf, "pre.txt", _PRE_COLUMNS):
                sub = filings.get(r["adsh"])
                if sub is None or r["inpth"] != "0" or r["stmt"] not in KEPT_STATEMENTS:
                    continue
                out[wanted[sub["cik"]]].append({
                    "accn": r["adsh"], "form": sub["form"], "period": sub["period"],
                    "filed": sub["filed"], "accepted": sub["accepted"], "statement": r["stmt"],
                    "tag": r["tag"], "version": r["version"], "plabel": r["plabel"],
                    "report": int(r["report"]), "line": int(r["line"])})
    except (zipfile.BadZipFile, KeyError, ValueError, OSError, EOFError) as exc:
        raise StatementsSourceError(f"{path.name}: {type(exc).__name__}: {exc}") from exc
    return out


def _extract(config: Config, ciks: Collection[str], quarters: Iterable[str],
             report: StatementsReport) -> None:
    wanted = {str(int(c)): normalize_cik(c) for c in ciks}
    per_issuer: dict[str, dict[tuple, dict]] = {c: {} for c in wanted.values()}
    for quarter in sorted(quarters):
        for cik, rows in _read_quarter(archive_root(config) / f"{quarter}.zip", wanted).items():
            kept = per_issuer[cik]
            for r in rows:
                key = (r["accn"], r["statement"], r["tag"])
                if key not in kept or (r["report"], r["line"]) < (kept[key]["report"], kept[key]["line"]):
                    kept[key] = r
    for cik, kept in per_issuer.items():
        lines = [json.dumps(kept[k], sort_keys=True) for k in sorted(kept)]
        path = statements_path(config, cik)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, "".join(f"{line}\n" for line in lines))
        report.issuers += 1
        report.rows += len(lines)


def _fetch_missing(config: Config, write: bool, fetcher, now: datetime,
                   report: StatementsReport) -> list[str]:
    """Quarters on disk after trying to fetch the missing ones."""
    expected, grace = ended_quarters(now.date(), config.statements_publish_grace_days)
    root, usable = archive_root(config), []
    for quarter in [*expected, *grace]:
        must_exist = quarter in expected
        if (root / f"{quarter}.zip").is_file():
            report.present += 1
            usable.append(quarter)
        elif not write:
            report.to_fetch.append(quarter)
        else:
            try:
                if _fetch_quarter(config, fetcher, quarter, now):
                    report.fetched += 1
                    usable.append(quarter)
                elif must_exist:
                    report.errors.append(f"{quarter}: expected and missing (404)")
                else:
                    report.not_published_yet.append(quarter)
            except (StatementsSourceError, requests.RequestException, OSError) as exc:
                report.errors.append(f"{quarter}: {type(exc).__name__}: {exc}")
    return usable


def fetch_statements(ciks: Collection[str], *, config: Config, write: bool, fetcher=None,
                     now: datetime | None = None) -> StatementsReport:
    """Archive missing quarters, then rebuild the per-issuer files. Dry-run says what is missing."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = StatementsReport(write=write)
    root, usable = archive_root(config), _fetch_missing(config, write, fetcher, now, report)
    if not write or report.errors:
        return report
    try:
        _extract(config, ciks, usable, report)
    except StatementsSourceError as exc:
        report.errors.append(str(exc))
        return report
    if not config.statements_keep_archives:
        for quarter in usable:
            (root / f"{quarter}.zip").unlink()
    return report


def refresh_statements(ciks: Collection[str], *, config: Config, fetcher,
                       now: datetime | None = None) -> StatementsReport:
    """Look for newly published quarters, and rebuild the files of the issuers older than the newest
    archive, so that a filing marked ``inferred`` gets its real positions at the next run. Does
    nothing until ``statements --write`` has set up the archive: the first 5.7 GB stay a deliberate
    step. With ``statements_keep_archives`` off there is nothing to compare, so nothing happens."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    report = StatementsReport(write=True)
    if not (archive_root(config) / MANIFEST).is_file() or not config.statements_keep_archives:
        return report
    usable = _fetch_missing(config, True, fetcher, now, report)
    if report.errors or not usable:
        return report
    newest = max((archive_root(config) / f"{q}.zip").stat().st_mtime for q in usable)
    stale = [c for c in ciks if not statements_path(config, c).is_file()
             or statements_path(config, c).stat().st_mtime < newest]
    if stale:
        try:
            _extract(config, stale, usable, report)
        except StatementsSourceError as exc:
            report.errors.append(str(exc))
    return report


def load_statements(config: Config, cik: str) -> list[dict] | None:
    """One issuer's rows, or None when ``statements`` has not produced its file."""
    path = statements_path(config, cik)
    if not path.is_file():
        return None
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _day(raw: str) -> date | None:
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except (TypeError, ValueError):
        return None


def statement_rows(cik: str, statements: Iterable[dict], *, acceptance: Mapping[str, datetime],
                   manifest_doc_ids: Collection[str], config: Config,
                   now: datetime) -> FundamentalsResult:
    """``filing_statements`` rows. known_at follows the same rule as the filing's facts."""
    clock = KnownAtClock.of(config, now)
    cik, counters = normalize_cik(cik), Counter()
    rows = []
    for s in statements:
        valid = _day(s["period"])
        if valid is None:
            counters["statements_dropped:bad_period"] += 1
            continue
        known_at, _ = clock.resolve(s["accn"], _day(s["filed"]), acceptance, counters)
        if known_at is None:
            continue
        rows.append({
            "entity_scheme": "cik", "entity_value": cik, "filing_ref": s["accn"],
            "statement": s["statement"], "tag": s["tag"], "tag_version": s["version"],
            "plabel": s["plabel"], "valid_date": valid, "known_at": known_at,
            "known_at_kind": "published", "source_code": SOURCE_CODE,
            "source_doc_id": source_doc_id(cik, s["form"], s["accn"], manifest_doc_ids, counters)})
    return FundamentalsResult(rows=rows, counters=counters)
