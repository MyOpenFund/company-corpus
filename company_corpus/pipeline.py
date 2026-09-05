"""Discovery orchestration with idempotent convergence.

Parallels ``cb_corpus.pipeline.run``. For each issuer in the universe, discover
filings (metadata only — downloads land in Phase 2) and merge them into the
per-issuer manifest. Multi-round crawling repeats until a round adds/updates
nothing and produces no new errors, matching cb_corpus's convergence contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from .config import Config, normalize_cik
from .entity import EntityRegistry
from .financials import normalized_rows, render_summary_html
from .http import Fetcher
from .models import FilingRecord, IdentityCollisionError
from .ownership import (
    find_ownership_doc,
    form345_rows,
    form345_text,
    parse_13f,
    parse_form345,
    render_13f_html,
    render_form345_html,
    thirteenf_rows,
    thirteenf_text,
)
from .sources.edgar_submissions import EdgarSubmissions
from .sources.edgar_xbrl import EdgarXBRL
from .storage import SaveStats, Storage
from .taxonomy import FULL_SCOPE, FormType


def _in_period(
    rec: FilingRecord,
    year_min: int | None,
    year_max: int | None,
    since: date | None,
    until: date | None,
) -> bool:
    """True if a record falls within the requested year/date window (all bounds AND-ed)."""
    y, d = rec.year, rec.filing_date
    if year_min is not None and (y is None or y < year_min):
        return False
    if year_max is not None and (y is None or y > year_max):
        return False
    if since is not None and (d is None or d < since):
        return False
    if until is not None and (d is None or d > until):
        return False
    return True


@dataclass
class RunReport:
    """Aggregate outcome of a discovery run."""

    rounds: int = 0
    issuers: int = 0
    stats: SaveStats = field(default_factory=SaveStats)
    errors: list[dict] = field(default_factory=list)


def discover_universe(
    ciks: Iterable[str],
    *,
    scope: Sequence[FormType] = FULL_SCOPE,
    since: date | None = None,
    dry_run: bool = True,
    max_rounds: int = 1,
    config: Config | None = None,
    fetcher: Fetcher | None = None,
    storage: Storage | None = None,
    entities: EntityRegistry | None = None,
    run_id: str | None = None,
) -> RunReport:
    """Discover filings for every CIK and merge into manifests.

    Idempotent: re-running with the same inputs converges (no changes). With
    ``dry_run=True`` nothing is persisted but the report reflects what would
    change.

    If an :class:`EntityRegistry` is supplied (or one exists on disk), the input
    CIKs are expanded through the alias/successor map so a single issuer pulls
    every CIK of its economic entity (e.g. Alphabet also crawls Google's old
    CIK), and each record is stamped with its ``entity_id``.

    ``run_id`` (here and on every other entry point in this module) stamps the
    rows appended to ``discovery_errors.jsonl`` so a trail row can be joined
    back to the run report that recorded it.
    """
    config = config or Config()
    fetcher = fetcher or Fetcher(config)
    storage = storage or Storage(config)
    if entities is None:
        entities = EntityRegistry(config).load()

    cik_list = entities.expand_all(ciks)

    report = RunReport(issuers=len(cik_list))
    for round_no in range(1, max_rounds + 1):
        report.rounds = round_no
        round_stats = SaveStats()
        round_errors: list[dict] = []

        for cik in cik_list:
            source = EdgarSubmissions(fetcher=fetcher, config=config)
            records = list(source.discover(cik, scope=scope, since=since))
            entity_id = entities.entity_id_for(cik)
            if entity_id:
                for rec in records:
                    rec.entity_id = entity_id
            round_stats += storage.save_records(records, dry_run=dry_run)
            round_errors.extend(source.errors)

        report.stats += round_stats
        if round_errors:
            report.errors.extend(round_errors)
            if not dry_run:
                storage.record_errors(round_errors, run_id=run_id)

        # Converged: a round changed nothing and hit no errors.
        if round_stats.added == 0 and round_stats.updated == 0 and not round_errors:
            break

    return report


@dataclass
class DownloadReport:
    """Aggregate outcome of a download run."""

    downloaded: int = 0
    repaired: int = 0        # half-processed documents re-derived from disk
    would_repair: int = 0    # dry run: documents that WOULD be repaired
    skipped: int = 0
    empty: int = 0
    errors: int = 0
    bytes: int = 0
    error_items: list[dict] = field(default_factory=list)


def download_universe(
    ciks: Iterable[str],
    *,
    scope: Sequence[FormType] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = True,
    overwrite: bool = False,
    limit: int | None = None,
    config: Config | None = None,
    fetcher: Fetcher | None = None,
    storage: Storage | None = None,
    run_id: str | None = None,
) -> DownloadReport:
    """Download + decompose filings already present in the issuers' manifests.

    Reads each issuer's manifest, fetches the complete submission for each record
    (optionally filtered by ``scope`` and a year/date window), decomposes it, and
    persists the updated record. Idempotent: already-downloaded filings are
    skipped.

    ``limit`` caps the number of *new downloads* across the run and nothing
    else: repairs are not capped by ``--limit`` (they cost no network -- they
    re-derive artefacts from bytes already on disk), so a limited run still
    converges every half-processed document it walks past.
    ``DownloadReport.repaired`` reports them, and they stay counted in
    ``docs_new`` because a repair adopts bytes that were previously unusable.
    """
    config = config or Config()
    fetcher = fetcher or Fetcher(config)
    storage = storage or Storage(config)
    scope_set = set(scope) if scope else None

    report = DownloadReport()
    for cik in ciks:
        manifest = storage.load_manifest(cik)
        records = [
            r for r in manifest.values()
            if (scope_set is None or r.form_type in scope_set)
            and _in_period(r, year_min, year_max, since, until)
        ]
        records.sort(key=lambda r: (r.filing_date or date.min), reverse=True)

        touched = []
        for rec in records:
            # Only `downloaded` is weighed against the limit: a repair costs no
            # network, so capping it would leave documents half-processed for no
            # gain (see the docstring).
            if limit is not None and report.downloaded >= limit:
                break
            res = storage.fetch_and_store(rec, fetcher, dry_run=dry_run, overwrite=overwrite)
            touched.append(rec)
            if res.status == "downloaded":
                report.downloaded += 1
                report.bytes += res.bytes
            elif res.status == "repaired":
                report.repaired += 1
            elif res.status == "would-repair":
                report.would_repair += 1
            elif res.status == "skipped":
                report.skipped += 1
            elif res.status == "error":
                report.errors += 1
                report.error_items.append(
                    {"source": "download", "context": rec.doc_id, "url": rec.submission_url, "error": res.error}
                )

        if not dry_run and touched:
            storage.save_records(touched, dry_run=False)

    if not dry_run and report.error_items:
        storage.record_errors(report.error_items, run_id=run_id)
    return report


@dataclass
class RenderReport:
    """Aggregate outcome of a PDF-render run."""

    rendered: int = 0
    would_render: int = 0
    skipped: int = 0
    no_primary: int = 0
    errors: int = 0
    error_items: list[dict] = field(default_factory=list)


def render_universe(
    ciks: Iterable[str],
    *,
    renderer=None,
    scope: Sequence[FormType] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = True,
    overwrite: bool = False,
    limit: int | None = None,
    config: Config | None = None,
    storage: Storage | None = None,
    run_id: str | None = None,
) -> RenderReport:
    """Render downloaded primary documents to PDF (separate batch).

    Walks each issuer's manifest and renders the primary document of every
    record that has been downloaded (Phase 2) but not yet rendered. ``renderer``
    defaults to a headless-Chrome renderer; pass one explicitly to override (or
    in tests). ``limit`` caps the number of *new* renders. Idempotent.
    """
    config = config or Config()
    storage = storage or Storage(config)
    scope_set = set(scope) if scope else None

    if renderer is None and not dry_run:
        # Imported lazily so dry-runs / tests don't require Chrome.
        from .render import make_chrome_renderer

        renderer = make_chrome_renderer()

    report = RenderReport()
    for cik in ciks:
        manifest = storage.load_manifest(cik)
        records = [
            r for r in manifest.values()
            if (scope_set is None or r.form_type in scope_set)
            and _in_period(r, year_min, year_max, since, until)
        ]
        records.sort(key=lambda r: (r.filing_date or date.min), reverse=True)

        touched = []
        for rec in records:
            if limit is not None and report.rendered >= limit:
                break
            res = storage.render_record(rec, renderer, dry_run=dry_run, overwrite=overwrite)
            touched.append(rec)
            if res.status == "rendered":
                report.rendered += 1
            elif res.status == "would-render":
                report.would_render += 1
            elif res.status == "skipped":
                report.skipped += 1
            elif res.status == "no-primary":
                report.no_primary += 1
            elif res.status == "error":
                report.errors += 1
                report.error_items.append(
                    {"source": "render", "context": rec.doc_id,
                     "url": rec.primary_doc_url, "error": res.error}
                )

        if not dry_run and touched:
            storage.save_records(touched, dry_run=False)

    if not dry_run and report.error_items:
        storage.record_errors(report.error_items, run_id=run_id)
    return report


@dataclass
class FinancialsReport:
    """Aggregate outcome of an XBRL financials run."""

    issuers: int = 0
    periods: int = 0
    stats: SaveStats = field(default_factory=SaveStats)
    errors: list[dict] = field(default_factory=list)


def fetch_financials(
    ciks: Iterable[str],
    *,
    since_year: int | None = None,
    until_year: int | None = None,
    dry_run: bool = True,
    config: Config | None = None,
    fetcher: Fetcher | None = None,
    storage: Storage | None = None,
    run_id: str | None = None,
) -> FinancialsReport:
    """Build per-period XBRL financial summaries (family F1) into manifests.

    For each issuer: fetch company facts, group into one summary per reporting
    period (annual/quarterly), and emit an F1 ``FilingRecord`` per period with the
    period's **publication date**. Persists the raw company-facts JSON (canonical),
    a normalized facts table, and an HTML summary per period (so the existing
    ``render_universe`` / ``rag.iter_items`` handle PDF + ingestion). The summaries
    feed the RAG; the raw JSON preserves exhaustivity.

    If two of an issuer's period summaries compute the same ``doc_id`` -- which
    would overwrite one period's summary with another's -- THAT issuer is skipped
    without writing anything and recorded as an error item in the report, rather
    than being deduped away silently (DI-C2) or aborting the whole run: the other
    issuers still produce, and the exit-code doctrine
    (:meth:`runreport.RunReport.finish`) decides what the run is worth.
    """
    config = config or Config()
    fetcher = fetcher or Fetcher(config)
    storage = storage or Storage(config)
    cik_list = list(ciks)

    report = FinancialsReport(issuers=len(cik_list))
    for cik in cik_list:
        source = EdgarXBRL(fetcher=fetcher, config=config)
        facts, summaries = source.period_summaries(
            cik, since_year=since_year, until_year=until_year)
        report.errors.extend(source.errors)
        if not facts or not summaries:
            continue

        records: list[FilingRecord] = []
        rows: list[dict] = []
        for ps in summaries:
            rec = FilingRecord(
                cik=cik, form_type=FormType.F1,
                sec_form=f"{ps.sec_form}/XBRL", accession=ps.accession,
                title=f"{ps.company} — {ps.period_label} financial summary",
                company=ps.company, company_current=ps.company_current,
                filing_date=ps.publication_date, period_of_report=ps.period_end,
                frequency=ps.frequency, provenance="edgar_xbrl",
            )
            records.append(rec)
            rows.extend(normalized_rows(cik, ps))

        # Identity is checked BEFORE anything is written: the summaries are named
        # by doc_id on disk and keyed by doc_id in the manifest, so two records
        # sharing an id silently overwrite one fiscal year with another (DI-C2).
        # The previous code wrote each summary inside the loop above, which
        # destroyed the artefact before any check could see the clash.
        try:
            _assert_unique_doc_ids(cik, records)
        except IdentityCollisionError as exc:
            # Fail loud, write nothing -- for THIS issuer only. A corrupt issuer
            # must not cost the run its other issuers' work.
            report.errors.append({"source": "edgar_xbrl", "context": normalize_cik(cik),
                                  "doc_ids": exc.doc_ids, "error": str(exc)})
            continue

        report.periods += len(records)
        if not dry_run:
            for rec, ps in zip(records, summaries, strict=True):
                storage.write_financial_summary(rec, render_summary_html(ps),
                                                _summary_text(ps))
            storage.store_companyfacts(cik, facts)
            storage.write_financials_table(cik, rows)
        report.stats += storage.save_records(records, dry_run=dry_run)

    if not dry_run and report.errors:
        storage.record_errors(report.errors, run_id=run_id)
    return report


def _assert_unique_doc_ids(cik: str, records: Sequence[FilingRecord]) -> None:
    """Raise :class:`IdentityCollisionError` if two records share a ``doc_id``.

    A shared id means one record's artefact overwrites the other's on disk and
    its manifest row, so the clash must never be deduped away silently (DI-C2).
    """
    ids = [r.doc_id for r in records]
    if len(set(ids)) == len(ids):
        return
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    raise IdentityCollisionError(
        f"{normalize_cik(cik)}: {len(ids) - len(set(ids))} colliding doc_id(s) among "
        f"{len(ids)} period summaries ({duplicates[:5]}). A collision "
        f"silently overwrites a fiscal year's summary and must fail loudly.",
        doc_ids=duplicates)


def _summary_text(ps) -> str:
    """Plain-text rendering of a period summary (text fallback for the RAG)."""
    lines = [f"{ps.company} — {ps.period_label} financial summary",
             f"Period ending {ps.period_end} ({ps.frequency}); "
             f"published (filed) {ps.publication_date}; "
             f"source {ps.sec_form} accession {ps.accession}", ""]
    for v in ps.values.values():
        lines.append(f"{v['label']}: {v['value']} {v['unit']}")
    derived = ps.derived
    if derived:
        lines += ["", "Derived metrics:"]
        lines += [f"{v['label']}: {v['value']} {v['unit']}" for v in derived.values()]
    return "\n".join(lines)


@dataclass
class OwnershipReport:
    """Aggregate outcome of an ownership (family E) run."""

    issuers: int = 0
    downloaded: int = 0
    would_download: int = 0   # dry run: filings that WOULD be downloaded
    repaired: int = 0         # half-processed submissions re-derived from disk
    would_repair: int = 0     # dry run: submissions that WOULD be repaired
    parsed_insider: int = 0   # E1 Form 3/4/5
    parsed_13f: int = 0       # E2 13F-HR
    passthrough: int = 0      # E3 SC 13D/G (narrative, generic text)
    errors: int = 0
    error_items: list[dict] = field(default_factory=list)


_OWNERSHIP_SCOPE = (FormType.E1, FormType.E2, FormType.E3)


def process_ownership(
    ciks: Iterable[str],
    *,
    scope: Sequence[FormType] | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = True,
    overwrite: bool = False,
    limit: int | None = None,
    config: Config | None = None,
    fetcher: Fetcher | None = None,
    storage: Storage | None = None,
    run_id: str | None = None,
) -> OwnershipReport:
    """Download + structure ownership filings already discovered in the manifests.

    Operates on family-E records (run ``discover --forms E --write`` first).
    Downloads each complete submission (canonical), then for Form 3/4/5 (E1) and
    13F (E2) parses the structured XML into a readable summary (overriding the
    poor raw-XML text) and appends normalized rows to ``data/ownership/<cik>.jsonl``.
    SC 13D/G (E3) keep the generic narrative text. Idempotent; ``limit`` caps new
    downloads (curated tier is the default usage).
    """
    config = config or Config()
    fetcher = fetcher or Fetcher(config)
    storage = storage or Storage(config)
    scope_set = set(scope) if scope else set(_OWNERSHIP_SCOPE)

    report = OwnershipReport()
    cik_list = list(ciks)
    report.issuers = len(cik_list)

    for cik in cik_list:
        manifest = storage.load_manifest(cik)
        records = [
            r for r in manifest.values()
            if r.form_type in scope_set
            and _in_period(r, year_min, year_max, since, until)
        ]
        records.sort(key=lambda r: (r.filing_date or date.min), reverse=True)

        touched: list[FilingRecord] = []
        rows: list[dict] = []
        for rec in records:
            if limit is not None and report.downloaded >= limit:
                break
            res = storage.fetch_and_store(rec, fetcher, dry_run=dry_run, overwrite=overwrite)
            touched.append(rec)
            if res.status == "error":
                report.errors += 1
                report.error_items.append(
                    {"source": "ownership", "context": rec.doc_id,
                     "url": rec.submission_url, "error": res.error})
                continue
            if res.status == "downloaded":
                report.downloaded += 1
            elif res.status == "would-download":
                report.would_download += 1
            elif res.status == "repaired":
                report.repaired += 1
            elif res.status == "would-repair":
                report.would_repair += 1
            if dry_run or not rec.local_path:
                continue

            raw = (config.data_dir / rec.local_path).read_text(encoding="utf-8", errors="replace")
            if rec.form_type is FormType.E1:
                xml = find_ownership_doc(raw, "E1")
                filing = parse_form345(xml) if xml else None
                if filing:
                    storage.write_ownership_summary(
                        rec, render_form345_html(filing), form345_text(filing))
                    rows.extend(form345_rows(rec.cik, rec.accession, filing))
                    report.parsed_insider += 1
            elif rec.form_type is FormType.E2:
                xml = find_ownership_doc(raw, "E2")
                if xml:
                    holdings, agg = parse_13f(xml)
                    agg["value_unit"] = "USD" if rec.filing_date >= date(2023, 1, 3) else "USD_thousands"
                    rep = (rec.period_of_report or rec.filing_date)
                    rep_str = rep.isoformat() if rep else ""
                    storage.write_ownership_summary(
                        rec,
                        render_13f_html(holdings, agg, filer=rec.company or rec.cik, report=rep_str),
                        thirteenf_text(holdings, agg, filer=rec.company or rec.cik, report=rep_str))
                    rows.extend(thirteenf_rows(rec.cik, rec.accession, holdings, rec.filing_date))
                    report.parsed_13f += 1
            else:  # E3 narrative — generic text from fetch_and_store is kept
                report.passthrough += 1

        if not dry_run:
            if touched:
                storage.save_records(touched, dry_run=False)
            if rows:
                storage.write_ownership_table(cik, rows)

    if not dry_run and report.error_items:
        storage.record_errors(report.error_items, run_id=run_id)
    return report
