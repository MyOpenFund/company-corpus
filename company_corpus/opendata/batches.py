"""``store-batches``: layer 0 -> open-data-store batches, one issuer at a time (spec §3, §6).

Per issuer: one ``entity_observations`` batch, then one ``fundamentals``, one
``filing_statements`` and one ``filings`` batch, written through the open-data-store writer into a
landing zone. Stateless: full history every run; the
store's publisher skips what it already holds. An issuer's failure never stops the others.
Dry-run (``write=False``) computes and validates everything from disk and writes nothing.

A submissions archive older than a filing its companyfacts cites is re-fetched once per run, on
``write`` without ``offline``: otherwise that filing would get the fallback ``known_at`` and a
later refresh would add a second vintage with an earlier ``known_at``.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ..concepts.coverage import coverage, positioned_tags
from ..concepts.dictionary import load_dictionary
from ..concepts.selection import run_selection
from ..config import Config, normalize_cik
from ..http import Fetcher
from ..storage import Storage
from .filings import filings_rows
from .fundamentals import (
    as_filed_points,
    flatten_with_namespace,
    fundamentals_rows,
    ingest_namespaces,
)
from .identity import identity_rows
from .statements import load_statements, refresh_statements, statement_rows, statements_path
from .submissions import (
    ArchiveCorrupt,
    acceptance_times,
    archive_dir,
    archived_accessions,
    fetch_archive,
    load_archive,
    main_name,
)

log = logging.getLogger(__name__)
PRODUCER = "company-corpus"


class StoreUnavailable(RuntimeError):
    """open-data-store (with fundamentals 3.x) is not installed."""


class _IssuerFailed(RuntimeError):
    pass


def contracts_api(*, needed_by: str = "store-batches", extra: str = "store") -> SimpleNamespace:
    """pyarrow and the four open-data-store contracts this producer fills (no writer)."""
    try:
        import pyarrow as pa
        from open_data_store.contracts.standard import (
            ENTITY_OBSERVATIONS,
            FILING_STATEMENTS,
            FILINGS,
            FUNDAMENTALS,
        )
    except ImportError as exc:
        raise StoreUnavailable(
            f"{needed_by} needs open-data-store: pip install 'myopenfund-company-corpus[{extra}]'") from exc
    if FUNDAMENTALS.major != 3 or "ytd9" not in (FUNDAMENTALS.column("frequency").allowed or ()):
        raise StoreUnavailable(
            f"{needed_by} needs fundamentals 3.1 or later (frequency ytd9), "
            f"found {FUNDAMENTALS.version}")
    return SimpleNamespace(pa=pa, ENTITY_OBSERVATIONS=ENTITY_OBSERVATIONS,
                           FUNDAMENTALS=FUNDAMENTALS, FILING_STATEMENTS=FILING_STATEMENTS,
                           FILINGS=FILINGS)


def _store_api() -> SimpleNamespace:
    try:
        from open_data_store.contracts.rules import BatchInvalid, conform
        from open_data_store.writer import write_batch
        from open_data_store.writer.batch import batch_id_for, content_hash
    except ImportError as exc:
        raise StoreUnavailable(
            "store-batches needs open-data-store: pip install 'myopenfund-company-corpus[store]'") from exc
    mod = contracts_api()
    mod.BatchInvalid, mod.conform, mod.write_batch = BatchInvalid, conform, write_batch
    mod.batch_id_for, mod.content_hash = batch_id_for, content_hash
    return mod


def contract_table(mod, contract, rows: list[dict]):
    """One contract's Arrow table from its rows, typed by the contract's own schema."""
    return mod.pa.Table.from_pylist(rows, schema=contract.schema)


@dataclass
class IssuerRows:
    """The four tables' rows of one issuer, and the row functions' counters."""
    identity: list[dict]
    fundamentals: list[dict]
    filings: list[dict]
    statements: list[dict]
    counters: Counter

    def by_contract(self, mod) -> tuple[tuple[object, list[dict]], ...]:
        return ((mod.ENTITY_OBSERVATIONS, self.identity), (mod.FUNDAMENTALS, self.fundamentals),
                (mod.FILINGS, self.filings), (mod.FILING_STATEMENTS, self.statements))


def issuer_rows(cik: str, archive, facts: dict, statements: list[dict], *,
                manifest_doc_ids, config: Config, now: datetime) -> IssuerRows:
    """The ``entity_observations``, ``fundamentals``, ``filings`` and ``filing_statements`` rows of
    one issuer. Shared by ``store-batches`` and the live (no-store) mode, so both compute alike."""
    acceptance = acceptance_times(archive)
    ident = identity_rows(archive, country_codes=config.edgar_country_codes)
    fund = fundamentals_rows(cik, facts, acceptance=acceptance,
                             manifest_doc_ids=manifest_doc_ids, config=config, now=now)
    placed = statement_rows(cik, statements, acceptance=acceptance,
                            manifest_doc_ids=manifest_doc_ids, config=config, now=now)
    filed = filings_rows(cik, archive, manifest_doc_ids=manifest_doc_ids, config=config, now=now)
    counters: Counter = Counter()
    counters.update(ident.counters)
    counters.update(fund.counters)
    counters.update(placed.counters)
    counters.update({f"filings:{k}": v for k, v in filed.counters.items()})
    return IssuerRows(identity=ident.rows, fundamentals=fund.rows, filings=filed.rows,
                      statements=placed.rows, counters=counters)


@dataclass
class IssuerOutcome:
    cik: str
    status: str = "ok"  # ok | failed | pending_fetch (dry-run, archive not fetched yet)
    error: str | None = None
    rows: dict[str, int] = field(default_factory=dict)
    batches: dict[str, str] = field(default_factory=dict)
    counters: Counter = field(default_factory=Counter)
    coverage: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict:
        d = {"status": self.status, "rows": dict(self.rows), "batches": dict(self.batches),
             "counters": dict(sorted(self.counters.items()))}
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class StoreBatchesReport:
    landing: str
    write: bool
    issuers: list[IssuerOutcome] = field(default_factory=list)
    statement_errors: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[IssuerOutcome]:
        return [o for o in self.issuers if o.status == "failed"]

    @property
    def pending(self) -> list[IssuerOutcome]:
        return [o for o in self.issuers if o.status == "pending_fetch"]

    @property
    def emitted(self) -> list[IssuerOutcome]:
        return [o for o in self.issuers if o.status == "ok"]

    def totals(self) -> dict:
        rows: Counter = Counter()
        counters: Counter = Counter()
        cover: Counter = Counter()
        for o in self.issuers:
            rows.update(o.rows)
            counters.update(o.counters)
            cover.update(o.coverage)
        return {"issuers": len(self.issuers), "ok": len(self.emitted),
                "failed": len(self.failed), "pending_fetch": len(self.pending),
                "rows": dict(sorted(rows.items())), "counters": dict(sorted(counters.items())),
                "coverage": dict(sorted(cover.items()))}

    def to_details(self) -> dict:
        return {"store_batches": {"landing": self.landing, "write": self.write,
                                  "totals": self.totals(),
                                  "issuers": {o.cik: o.to_dict() for o in self.issuers}}}


def store_batches(ciks, *, config: Config, landing: Path, write: bool, offline: bool = False,
                  refresh: bool = False, fetcher=None, run_id: str,
                  now: datetime | None = None) -> StoreBatchesReport:
    mod = _store_api()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if fetcher is None and write and not offline:
        fetcher = Fetcher(config)
    rep = StoreBatchesReport(landing=str(landing), write=write)
    if write and not offline:
        rep.statement_errors = refresh_statements(ciks, config=config, fetcher=fetcher, now=now).errors
    storage = Storage(config)
    for cik in ciks:
        out = IssuerOutcome(normalize_cik(cik))
        rep.issuers.append(out)
        try:
            _issuer(out, mod, config=config, storage=storage, landing=Path(landing),
                    write=write, offline=offline, refresh=refresh, fetcher=fetcher,
                    run_id=run_id, now=now)
        except _IssuerFailed as exc:
            out.status, out.error = "failed", str(exc)
        except mod.BatchInvalid as exc:
            out.status, out.error = "failed", f"batch_invalid: {'; '.join(exc.reasons)}"
            log.error("store-batches %s: batch refused by its contract (producer bug): %s",
                      out.cik, exc.reasons)
        except OSError as exc:
            out.status, out.error = "failed", f"io_error: {type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - an issuer's failure never stops the others
            out.status, out.error = "failed", f"unexpected: {type(exc).__name__}: {exc}"
            log.exception("store-batches %s: unexpected failure", out.cik)
    return rep


def _is_stale(archive, facts: dict, config: Config, now: datetime) -> bool:
    """True when a point of ``facts`` comes from a filing the archive does not list and that
    can have been accepted after the archive was fetched: its filing date is on or after the
    fetch day in EDGAR's zone (``store_edgar_day_tz``). A filing date is typically not before
    the acceptance date in Eastern time; a retroactive adjustment is the exception, and costs
    one fallback vintage, without conflict. An older missing accession stays missing whatever
    the archive's age, so it never counts: it would refresh on every run forever. A filing
    date more than ``store_stale_filed_max_days_ahead`` days after the RUN day is
    implausible and ignored (an old archive with a recent filing is the legitimate case)."""
    tz = ZoneInfo(config.store_edgar_day_tz)
    fetched_day = archive.fetched_at.astimezone(tz).date()
    latest = now.astimezone(tz).date() + timedelta(days=config.store_stale_filed_max_days_ahead)
    listed = archived_accessions(archive)
    d = load_dictionary()
    points = as_filed_points(flatten_with_namespace(facts, ingest_namespaces(d)),
                             config.store_duration_bands, Counter(),
                             config.store_currency_anchor_tags, dictionary=d,
                             report_forms=config.store_report_forms)
    return any(pt.accn not in listed and pt.filed is not None
               and fetched_day <= pt.filed <= latest
               for pt in points)


def _fetch(out: IssuerOutcome, config: Config, fetcher, now):
    try:
        return fetch_archive(config, out.cik, fetcher, now=now)
    except Exception as exc:  # noqa: BLE001 - any fetch failure is this issuer's
        raise _IssuerFailed(f"submissions_fetch_failed: {type(exc).__name__}: {exc}") from exc


def _archive(out: IssuerOutcome, config: Config, *, facts, write, offline, refresh, fetcher, now):
    present = (archive_dir(config, out.cik) / main_name(out.cik)).is_file()
    online = write and not offline
    if online and (refresh or not present):
        return _fetch(out, config, fetcher, now)
    if not present:
        if offline:
            raise _IssuerFailed("submissions_archive_missing (--offline)")
        out.status = "pending_fetch"
        return None
    try:
        archive = load_archive(config, out.cik)
    except ArchiveCorrupt as exc:
        raise _IssuerFailed(f"submissions_archive_corrupt: {exc} "
                            "(re-fetch it with --refresh-submissions --write)") from exc
    if not _is_stale(archive, facts, config, now):
        return archive
    if not online:
        out.counters["submissions_stale"] += 1
        return archive
    archive = _fetch(out, config, fetcher, now)
    out.counters["submissions_refreshed:stale"] += 1
    return archive


def _coverage(out: IssuerOutcome, mod, fund_rows: list[dict], position_rows: list[dict]) -> None:
    """The reader's selection on this issuer's rows, for the run report only."""
    facts = contract_table(mod, mod.FUNDAMENTALS, fund_rows)
    positions = contract_table(mod, mod.FILING_STATEMENTS, position_rows)
    d = load_dictionary()
    on_statement = positioned_tags(((r["filing_ref"], r["tag"], r["tag_version"])
                                    for r in position_rows), d)
    out.coverage.update(coverage(run_selection(facts, positions, d), on_statement, d))


def _issuer(out: IssuerOutcome, mod, *, config, storage, landing, write, offline, refresh,
            fetcher, run_id, now) -> None:
    facts_path = config.raw_dir / out.cik / "F1" / "companyfacts.json"
    if not facts_path.is_file():
        raise _IssuerFailed("companyfacts_missing")
    facts_bytes = facts_path.read_bytes()
    try:
        facts = json.loads(facts_bytes)
    except ValueError as exc:
        raise _IssuerFailed(f"companyfacts_unreadable: {exc}") from exc
    archive = _archive(out, config, facts=facts, write=write, offline=offline, refresh=refresh,
                       fetcher=fetcher, now=now)
    if archive is None:
        return
    manifest_doc_ids = set(storage.load_manifest(out.cik))
    statements = load_statements(config, out.cik)
    if statements is None:
        out.counters["statements_missing"] += 1
    rows = issuer_rows(out.cik, archive, facts, statements or [],
                       manifest_doc_ids=manifest_doc_ids, config=config, now=now)
    out.counters.update(rows.counters)
    _coverage(out, mod, rows.fundamentals, rows.statements)
    facts_rel = str(facts_path.relative_to(config.data_dir))
    inputs = [f"{facts_rel}@sha256:{hashlib.sha256(facts_bytes).hexdigest()}",
              *(f"{f.rel_path}@sha256:{f.sha256}" for f in archive.files)]
    if statements is not None:
        path = statements_path(config, out.cik)
        inputs.append(f"{path.relative_to(config.data_dir)}"
                      f"@sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}")
    for contract, contract_rows in rows.by_contract(mod):
        out.rows[contract.table_name] = len(contract_rows)
        if not contract_rows:
            continue
        table = contract_table(mod, contract, contract_rows)
        if write:
            ref = mod.write_batch(contract, table, landing_root=landing, run_id=run_id,
                                  producer=PRODUCER, source_inputs=inputs, now=now)
            out.batches[contract.table_name] = ref.batch_id
        else:
            t = mod.conform(contract, table, now=now)
            out.batches[contract.table_name] = mod.batch_id_for(contract, mod.content_hash(t))
