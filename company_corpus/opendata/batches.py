"""``store-batches``: layer 0 -> my-open-data batches, one issuer at a time (spec §3, §6).

Per issuer: one ``entity_observations`` batch, then one ``fundamentals`` batch, written
through the my-open-data writer into a landing zone. Stateless: full history every run; the
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

from ..config import Config, normalize_cik
from ..http import Fetcher
from ..storage import Storage
from .fundamentals import flatten_with_namespace, fundamentals_rows, select_points
from .identity import identity_rows
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
    """my-open-data (with fundamentals 2.x) is not installed."""


class _IssuerFailed(RuntimeError):
    pass


def _myopendata() -> SimpleNamespace:
    try:
        import pyarrow as pa
        from myopendata.contracts.rules import BatchInvalid, conform
        from myopendata.contracts.standard import ENTITY_OBSERVATIONS, FUNDAMENTALS
        from myopendata.writer import write_batch
        from myopendata.writer.batch import batch_id_for, content_hash
    except ImportError as exc:
        raise StoreUnavailable(
            "store-batches needs my-open-data: pip install 'company-corpus[store]'") from exc
    if FUNDAMENTALS.major != 2:
        raise StoreUnavailable(f"store-batches needs fundamentals 2.x, found {FUNDAMENTALS.version}")
    return SimpleNamespace(pa=pa, BatchInvalid=BatchInvalid, conform=conform,
                           ENTITY_OBSERVATIONS=ENTITY_OBSERVATIONS, FUNDAMENTALS=FUNDAMENTALS,
                           write_batch=write_batch, batch_id_for=batch_id_for,
                           content_hash=content_hash)


@dataclass
class IssuerOutcome:
    cik: str
    status: str = "ok"  # ok | failed | pending_fetch (dry-run, archive not fetched yet)
    error: str | None = None
    rows: dict[str, int] = field(default_factory=dict)
    batches: dict[str, str] = field(default_factory=dict)
    counters: Counter = field(default_factory=Counter)

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
        for o in self.issuers:
            rows.update(o.rows)
            counters.update(o.counters)
        return {"issuers": len(self.issuers), "ok": len(self.emitted),
                "failed": len(self.failed), "pending_fetch": len(self.pending),
                "rows": dict(sorted(rows.items())), "counters": dict(sorted(counters.items()))}

    def to_details(self) -> dict:
        return {"store_batches": {"landing": self.landing, "write": self.write,
                                  "totals": self.totals(),
                                  "issuers": {o.cik: o.to_dict() for o in self.issuers}}}


def store_batches(ciks, *, config: Config, landing: Path, write: bool, offline: bool = False,
                  refresh: bool = False, fetcher=None, run_id: str,
                  now: datetime | None = None) -> StoreBatchesReport:
    mod = _myopendata()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if fetcher is None and write and not offline:
        fetcher = Fetcher(config)
    rep = StoreBatchesReport(landing=str(landing), write=write)
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
    points = select_points(flatten_with_namespace(facts), config.store_duration_bands, Counter())
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
    ident = identity_rows(archive, country_codes=config.edgar_country_codes)
    fund = fundamentals_rows(out.cik, facts, acceptance=acceptance_times(archive),
                             manifest_doc_ids=set(storage.load_manifest(out.cik)),
                             config=config, now=now)
    out.counters.update(ident.counters)
    out.counters.update(fund.counters)
    facts_rel = str(facts_path.relative_to(config.data_dir))
    inputs = [f"{facts_rel}@sha256:{hashlib.sha256(facts_bytes).hexdigest()}",
              *(f"{f.rel_path}@sha256:{f.sha256}" for f in archive.files)]
    for contract, rows in ((mod.ENTITY_OBSERVATIONS, ident.rows), (mod.FUNDAMENTALS, fund.rows)):
        out.rows[contract.table_name] = len(rows)
        if not rows:
            continue
        table = mod.pa.Table.from_pylist(rows, schema=contract.schema)
        if write:
            ref = mod.write_batch(contract, table, landing_root=landing, run_id=run_id,
                                  producer=PRODUCER, source_inputs=inputs, now=now)
            out.batches[contract.table_name] = ref.batch_id
        else:
            t = mod.conform(contract, table, now=now)
            out.batches[contract.table_name] = mod.batch_id_for(contract, mod.content_hash(t))
