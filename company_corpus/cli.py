"""Command-line interface for company_corpus.

Mirrors cb_corpus CLI ergonomics: subcommands, a dry-run-by-default posture, and
an explicit ``--write`` flag for side effects. Phase 0 added inspection
commands; Phase 1 adds the issuer universe and EDGAR discovery.

Silent-failure doctrine — what each work command reports
--------------------------------------------------------
Every command in :data:`REPORTING_CMDS` folds its own counters into the run
report (``data/runs.jsonl``) through :func:`_feed_report`, so a run that did no
useful work can never exit 0. ``docs_new`` is the command's *unit of useful
work*, ``docs_seen`` what it examined, ``docs_failed`` its error count; every
error message is also recorded as a fetch error (capped samples), and a backend
that reports a truncated result sets ``truncated`` — which degrades the run even
when documents were written, because a partial listing must never look complete.

===================== ========== ============================ ======================= ==================
command               source     docs_seen                    docs_new (useful work)  docs_failed
===================== ========== ============================ ======================= ==================
discover              sec        manifest records seen        records added           discovery errors
  (with --download)   sec        + files examined             + files downloaded      + download errors
discover-index        sec        manifest records seen        records added           index errors
download              sec        downloaded+skipped+empty+err files downloaded        download errors
render-pdf            sec        rendered+would+skipped+
                                 no_primary+errors            rendered + would_render render errors
xbrl                  sec        issuers processed            period summaries        companyfacts errors
ownership             sec        issuers processed            insider+13F+passthrough ownership errors
                                                              + would_download (dry)
enrich-openfigi       sec        identifiers submitted        identifiers mapped      (none: see below)
eu-financials         xbrlorg    entities resolved            period summaries        ``out["errors"]``
eu-acquire            per        entities dispatched to       documents kept from     that backend's
                      backend    that backend                 that backend            errors
register-financials   per        entities resolved            period summaries        ``out["errors"]``
                      register
===================== ========== ============================ ======================= ==================

Three deliberate choices:

* **Dry runs count what WOULD be written.** ``render-pdf`` folds ``would_render``
  into ``docs_new``, ``ownership`` folds ``would_download``, and ``xbrl`` /
  ``eu-financials`` / ``register-financials`` count the period summaries they
  computed, so a dry run that found candidates and hit no errors is ``ok`` while
  a dry run with errors and zero candidates is ``degraded`` — never a green
  "nothing to do". ``download`` is the one exception: its producer has no
  would-download counter, so a dry-run download reports ``docs_new=0``.
* **``enrich-openfigi`` is recorded under ``sec``.** OpenFIGI is a mapping
  service, not a document authority, so it gets no code of its own (``one code
  per real-world regulatory authority``, see :mod:`company_corpus.source_codes`);
  the command exists to triage identifier CSVs into the SEC-anchored universe, so
  its counters land there. A no-match is *not* an error — OpenFIGI legitimately
  has no record for many private placements — so ``docs_failed`` stays 0 and a
  fully unmatched file is a clean "nothing new", not a failure.
* **``eu-financials`` reports ``xbrlorg``.** Its rows are tagged ``source="esef"``
  and come from the filings.xbrl.org aggregator (:mod:`company_corpus.eu.financials`),
  not from a national OAM backend. **``eu-acquire`` reports one row per backend**
  it dispatched to (``out["sources"]``, keyed by the backend ``name`` and resolved
  to the authority's code), so a dead national OAM is counted as that backend's
  own failure on its own row while the aggregator's row stays clean — never a
  single blended row. The run itself degrades only under the zero-useful-work
  rule of :meth:`RunReport.finish` (no document acquired at all): a dead OAM
  next to a productive aggregator is an ``ok`` run whose report names the dead
  authority. A spec that resolves to no LEI reaches no backend and no row; the
  command prints such specs (``unresolved: …``) and fails the run only when
  *every* spec is unresolved (bad inputs, or GLEIF unreachable).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from . import __version__
from .completeness import build_matrix, summarize
from .config import Config, normalize_cik
from .entity import EntityRegistry
from .http import Fetcher
from .pipeline import (
    discover_universe,
    download_universe,
    fetch_financials,
    process_ownership,
    render_universe,
)
from .eu.acquire import acquire
from .eu.financials import build_eu_financials
from .registers.financials import (
    build_be_financials,
    build_be_financials_from_files,
    build_ch_financials,
    build_dk_financials,
    build_dk_financials_from_files,
    build_ee_financials,
    build_ee_financials_from_files,
    build_fi_financials,
    build_fi_financials_from_files,
    build_lu_financials_from_files,
    build_register_financials,
    build_sk_financials,
    build_sk_financials_from_files,
)
from .openfigi import coverage_hint, map_identifiers
from .rag import iter_items
from .runreport import RunReport as DoctrineReport
from .source_codes import source_code_for
from .sources.cik_lookup import fetch_cik_lookup, parse_cik_lookup
from .sources.edgar_fts import EdgarFTS
from .sources.edgar_index import EdgarFullIndex
from .storage import Storage
from .taxonomy import FULL_SCOPE, parse_scope
from .universe import (
    Issuer,
    Universe,
    issuers_from_sp500,
    load_company_tickers,
    load_cusip_crosswalk,
    load_name_cache,
    read_identifier_csv,
    reconcile_identifiers,
    resolve_ciks,
    resolve_tickers,
    write_cusip_crosswalk,
    write_name_cache,
)


# Work commands that build a run-report, feed it through the pipeline, and
# ALWAYS write it (even on crash) before returning the doctrine exit code.
# Non-work commands (list-* / inspection / migrations, ...) are unaffected.
# A work command's non-zero return value (e.g. a caught RuntimeError reported
# as exit 1) is folded into the doctrine as a "failed" outcome rather than
# being silently discarded and reported as "ok".
REPORTING_CMDS = {
    "discover", "discover-index", "download", "render-pdf", "xbrl",
    "ownership", "enrich-openfigi", "eu-financials", "register-financials",
    "eu-acquire",
}


def _runs_path(args: argparse.Namespace | None = None) -> Path:
    """Where the run-report lands: COMPANY_DATA_DIR override (tests) wins;
    else ``--data-dir`` when given on ``args``; else Config's own data_dir.
    Config itself doesn't honor an env var for data_dir today, so
    COMPANY_DATA_DIR is this CLI's own override, not a pre-existing Config
    mechanism."""
    base = os.environ.get("COMPANY_DATA_DIR")
    if base:
        data_dir = Path(base)
    elif args is not None and getattr(args, "data_dir", None):
        data_dir = Path(args.data_dir)
    else:
        data_dir = Config().data_dir
    return data_dir / "runs.jsonl"


def _error_messages(items) -> list[str]:
    """Flatten a producer's error payloads (dicts or strings) to messages."""
    msgs: list[str] = []
    for item in items or ():
        if isinstance(item, dict):
            context = item.get("context") or item.get("url") or item.get("source") or ""
            text = item.get("error") or json.dumps(item, default=str, ensure_ascii=False)
            msgs.append(f"{context}: {text}" if context else str(text))
        else:
            msgs.append(str(item))
    return msgs


def _feed_report(
    report,
    code: str,
    *,
    seen: int = 0,
    new: int = 0,
    failed: int = 0,
    errors=(),
    truncated: bool = False,
) -> None:
    """Fold one command's counters into the run report under ``code``.

    ``report`` is ``None`` when a ``_cmd_*`` is called directly (older tests and
    library callers bypass :func:`main`, which is what attaches the report) — a
    no-op then, so feeding never becomes a precondition for running a command.

    ``code`` is any producer/backend tag; it is resolved through
    :func:`company_corpus.source_codes.source_code_for`, which raises rather than
    guess, so a new backend cannot quietly report under an invented authority.
    Each error message is recorded as a fetch error *and* counted in
    ``failed``: for these commands an error means both "a document was not
    produced" and "an upstream call failed". ``truncated`` marks the source as a
    partial listing (degrades the run on its own); a truncation with no error
    payload still records one synthetic message so the report says why.
    """
    if report is None:
        return
    stats = report.source(source_code_for(code))
    stats.docs_seen += seen
    stats.docs_new += new
    stats.docs_failed += failed
    msgs = _error_messages(errors)
    if truncated and not msgs:
        msgs = [f"{code}: backend reported a truncated result"]
    for msg in msgs:
        stats.record_fetch_error(msg, truncated=truncated)


def _feed_from_out(report, code: str, out: dict) -> None:
    """Feed the report from a producer ``out`` dict (EU + register builders).

    Their shared shape is ``entities`` / ``with_financials`` / ``no_financials`` /
    ``periods`` / ``errors`` (an int, with the messages in ``error_items``; a list
    of items is accepted too, as ``eu.acquire`` returns). A missing entity or a
    filing without financials is coverage, not an error — it is recorded in the
    coverage file and only ever counted in ``docs_seen``. A DEAD SOURCE is not:
    it arrives as an ``error_items`` entry (``status: "source-error"`` in the
    coverage file) and becomes a fetch error here, so an issuer whose register
    could not be read never passes for one that filed nothing.
    ``truncated`` is read when a backend grows it.
    """
    errors = out.get("errors") or 0
    if isinstance(errors, (list, tuple)):
        failed, items = len(errors), errors
    else:
        failed, items = int(errors), out.get("error_items") or ()
    _feed_report(
        report, code,
        seen=int(out.get("entities") or 0),
        new=int(out.get("periods") or 0),
        failed=failed,
        errors=items,
        truncated=bool(out.get("truncated")),
    )


def _run_id(args: argparse.Namespace) -> str | None:
    """The current run's id, or None when no report is attached (direct calls)."""
    return getattr(getattr(args, "report", None), "run_id", None)


def _record_out_errors(args: argparse.Namespace, config, out: dict) -> None:
    """Append a producer's ``error_items`` to the discovery-error trail.

    Every pillar writes to the same ``discovery_errors.jsonl``, stamped by
    :meth:`Storage.record_errors` with a timestamp and this run's id — so a dead
    EU or register source leaves the same auditable trace the SEC pillar leaves,
    instead of living only in the run report's capped samples. A no-op when the
    producer reported nothing, and on a dry run (no ``--write``): the run report
    still carries the errors, but "DRY-RUN (nothing written)" means nothing —
    the SEC pillar gates its trail on the same flag. Every caller defines
    ``--write``, so a namespace without it is a programming error and raises
    rather than silently reading as a dry run.
    """
    if not args.write:
        return
    items = out.get("error_items") or []
    if not items:
        return
    Storage(config).record_errors(items, run_id=_run_id(args))


def _parse_years(spec: str | None) -> list[int]:
    """Expand ``"2006-2025"`` / ``"2024"`` / ``"2006-2025,2026"`` to a year list."""
    if not spec:
        this_year = date.today().year
        return list(range(this_year - 19, this_year + 1))  # default: last 20 years
    years: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo, hi = token.split("-", 1)
            years.update(range(int(lo), int(hi) + 1))
        else:
            years.add(int(token))
    return sorted(years)


def _period_args(args: argparse.Namespace):
    """Parse --years / --since / --until into (year_min, year_max, since, until).

    ``--years`` (``YYYY`` or ``YYYY-YYYY``) bounds by filing year; ``--since`` /
    ``--until`` (``YYYY-MM-DD``) add exact-date bounds. All optional, AND-combined.
    """
    year_min = year_max = None
    if getattr(args, "years", None):
        ys = _parse_years(args.years)
        year_min, year_max = min(ys), max(ys)
    since = date.fromisoformat(args.since) if getattr(args, "since", None) else None
    until = date.fromisoformat(args.until) if getattr(args, "until", None) else None
    return year_min, year_max, since, until


def _add_period_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--years", default=None, help="filing-year filter, e.g. 2010-2020 or 2024")
    p.add_argument("--since", default=None, help="start date filter (YYYY-MM-DD)")
    p.add_argument("--until", default=None, help="end date filter (YYYY-MM-DD)")


def _config(args: argparse.Namespace) -> Config:
    """Build a Config, honoring the top-level --data-dir / --contact overrides.

    Both default to None on the namespace, so an unset flag falls through to the
    Config defaults (``./data`` and the COMPANY_CORPUS_CONTACT env var).
    """
    kw: dict = {}
    if getattr(args, "data_dir", None):
        kw["data_dir"] = args.data_dir
    if getattr(args, "contact", None):
        kw["contact"] = args.contact
    if getattr(args, "insecure", False):
        kw["verify_tls"] = False
    return Config(**kw)


def _ciks_for(args: argparse.Namespace, config: Config) -> list[str]:
    """Resolve the CIK set from --ciks or --universe."""
    if getattr(args, "ciks", None):
        return [normalize_cik(c) for c in args.ciks.split(",") if c.strip()]
    if getattr(args, "universe", None):
        return list(Universe(config).iter_ciks(args.universe))
    raise SystemExit("error: provide --ciks or --universe")


# ---- commands ----
def _cmd_list_forms(args: argparse.Namespace) -> int:
    scope = parse_scope(args.forms)
    default = set(FULL_SCOPE)
    print(f"{'code':<5} {'family':<7} {'in_default':<11} edgar_forms / label")
    print("-" * 72)
    for ft in scope:
        forms = ", ".join(ft.edgar_forms) if ft.edgar_forms else "(structured/XBRL)"
        flag = "yes" if ft in default else "opt-in"
        print(f"{ft.code:<5} {ft.family:<7} {flag:<11} {ft.label}  [{forms}]")
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    cfg = _config(args)
    print(f"version           : {__version__}")
    print(f"data_dir          : {cfg.data_dir}")
    print(f"user_agent        : {cfg.user_agent}")
    print(f"requests_per_sec  : {cfg.requests_per_second} (SEC max 10)")
    print(f"min_delay_seconds : {cfg.min_delay_seconds:.4f}")
    print(f"verify_tls        : {cfg.verify_tls}")
    print(f"manifest_dir      : {cfg.manifest_dir}")
    print(f"universe_dir      : {cfg.universe_dir}")
    print(f"raw_dir           : {cfg.raw_dir}")
    if not cfg.contact:
        print(
            "\nNo contact set: the User-Agent carries no email address. "
            "Set COMPANY_CORPUS_CONTACT before any live crawl "
            "(e.g. export COMPANY_CORPUS_CONTACT=you@example.com)."
        )
    return 0


def _cmd_enrich_openfigi(args: argparse.Namespace) -> int:
    """Enrich an identifier CSV via OpenFIGI (name/type/exchange + triage).

    Reported under the ``sec`` source: OpenFIGI is a mapping service, not a
    document authority, so it has no code of its own, and this command's job is
    to triage identifier files into the SEC-anchored universe. A no-match is not
    an error (OpenFIGI has no record for many private placements), so it counts
    only in ``docs_seen``.
    """
    path = Path(args.from_file)
    with path.open(encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = reader.fieldnames or []
        col = args.id_col or next(
            (h for h in headers if h.lower().strip() == args.id_type), None)
        if not col:
            raise SystemExit(f"error: no '{args.id_type}' column in {headers}; use --id-col")
        ids = []
        seen: set[str] = set()
        for row in reader:
            v = (row.get(col) or "").strip()
            if v and v not in seen:
                seen.add(v)
                ids.append(v)

    api_key = args.api_key or os.environ.get("OPENFIGI_API_KEY")
    records = map_identifiers(ids, id_type=args.id_type, api_key=api_key)
    hits = sum(1 for r in records.values() if r)
    hint_tally: dict[str, int] = {}
    for r in records.values():
        h = coverage_hint(r.security_type) if r else "no_match"
        hint_tally[h] = hint_tally.get(h, 0) + 1
    print(f"OpenFIGI: {hits}/{len(ids)} matched; triage {hint_tally}", file=sys.stderr)
    _feed_report(getattr(args, "report", None), "sec", seen=len(ids), new=hits)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["identifier", "name", "ticker", "security_type", "exch_code", "coverage_hint"])
            for ident in ids:
                r = records.get(ident)
                if r:
                    w.writerow([ident, r.name, r.ticker, r.security_type, r.exch_code,
                                coverage_hint(r.security_type)])
                else:
                    w.writerow([ident, "", "", "", "", "no_match"])
        print(f"wrote {len(ids)} enriched rows -> {out}")
    else:
        print("[no --out] re-run with --out PATH to write the enriched CSV")
    return 0


def _name_tier(args, cfg, fetcher):
    """Return ``(name_index, name_cache, ledger_path)`` for the name->CIK tier,
    or ``(None, None, ledger_path)`` when disabled. Fetches the cached
    cik-lookup file (one GET) and loads the durable ledger."""
    ledger_path = getattr(args, "name_cache", None) or str(cfg.name_cache_path)
    if getattr(args, "no_name_resolution", False):
        return None, None, ledger_path
    try:
        text = fetch_cik_lookup(fetcher, cfg.cik_lookup_path)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: name resolution unavailable (cik-lookup fetch failed: {exc}); "
            "building from ticker/CUSIP only",
            file=sys.stderr,
        )
        return None, None, ledger_path
    return parse_cik_lookup(text), load_name_cache(ledger_path), ledger_path


def _cmd_build_universe(args: argparse.Namespace) -> int:
    cfg = _config(args)
    fetcher = Fetcher(cfg)

    if getattr(args, "from_file", None):
        return _build_universe_from_file(args, cfg, fetcher)

    equity_index = getattr(args, "equity_index", None)
    if getattr(args, "index_legacy", None):
        print("NOTE: --index is deprecated; use --equity-index.", file=sys.stderr)
        equity_index = equity_index or args.index_legacy

    if equity_index:
        if equity_index != "sp500":
            raise SystemExit("error: only --equity-index sp500 is supported (see README)")
        name = args.name if args.name != "curated" else "sp500"
        if args.current_only:
            name_index = name_cache = None
            ledger_path = getattr(args, "name_cache", None) or str(cfg.name_cache_path)
        else:
            name_index, name_cache, ledger_path = _name_tier(args, cfg, fetcher)
        issuers, changes, unresolved = issuers_from_sp500(
            fetcher, start=args.since, current_only=args.current_only,
            name_index=name_index, name_cache=name_cache)
        if name_index is not None:
            pairs = [(it.company, it.cik) for it in issuers
                     if it.resolution == "name" and it.cik and it.company]
            if pairs:
                total = write_name_cache(ledger_path, pairs)
                print(f"name-cache: merged {len(pairs)} name->CIK pin(s) -> "
                      f"{ledger_path} ({total} total)", file=sys.stderr)
        mode = "historical union" if not args.current_only else "current snapshot"
        if unresolved:
            print(f"NOTE: {len(unresolved)} member(s) without a resolvable CIK "
                  f"(likely delisted; recorded with cik=\"\"): {', '.join(unresolved[:15])}"
                  f"{' …' if len(unresolved) > 15 else ''}", file=sys.stderr)
        if args.write:
            uni = Universe(cfg)
            path = uni.save(name, issuers)
            crows = 0
            if changes:
                cpath = uni.path(name).with_name(f"{name}_changes.jsonl")
                with cpath.open("w", encoding="utf-8") as fh:
                    for ch in changes:
                        fh.write(json.dumps(ch, ensure_ascii=False) + "\n")
                crows = len(changes)
            print(f"wrote {len(issuers)} issuers ({mode}) -> {path}"
                  + (f"; {crows} dated changes -> {cpath}" if crows else ""))
        else:
            resolved = sum(1 for it in issuers if it.cik)
            print(f"[dry-run] S&P 500 {mode}: {len(issuers)} members "
                  f"({resolved} with CIK, {len(issuers) - resolved} unresolved), "
                  f"{len(changes)} dated changes. Re-run with --write to persist.")
        return 0

    issuers: list = []
    if args.tickers:
        tickers = [t for t in args.tickers.split(",") if t.strip()]
        resolved, unresolved = resolve_tickers(tickers, fetcher)
        issuers.extend(resolved)
        if unresolved:
            print(f"WARNING: unresolved tickers (delisted/renamed?): {', '.join(unresolved)}",
                  file=sys.stderr)
    if args.ciks:
        # CIK-anchored: works for delisted/merged issuers the ticker map omits.
        issuers.extend(resolve_ciks([c for c in args.ciks.split(",") if c.strip()], fetcher))
    if not issuers:
        raise SystemExit("error: provide --tickers and/or --ciks")
    if args.write:
        path = Universe(cfg).save(args.name, issuers)
        print(f"wrote {len(issuers)} issuers -> {path}")
    else:
        print(f"[dry-run] resolved {len(issuers)} issuers for universe '{args.name}':")
        for it in issuers:
            print(f"  {it.cik}  {it.ticker:<8} {it.company}")
        print("re-run with --write to persist")
    return 0


def _build_universe_from_file(args: argparse.Namespace, cfg, fetcher) -> int:
    """Build a universe from a CSV of identifiers (CIK and/or ticker and/or CUSIP).

    Authority CIK > CUSIP > ticker. When ticker->CIK and CUSIP6->CIK disagree it's
    a collision (kept by default with --prefer cusip; --drop-collisions excludes).
    """
    rows = read_identifier_csv(args.from_file, ticker_col=args.ticker_col,
                               cusip_col=args.cusip_col, cik_col=args.cik_col)
    has_cusip = any(r["cusip6"] for r in rows)
    crosswalk: dict[str, set[str]] = {}
    if args.crosswalk:
        crosswalk = load_cusip_crosswalk(args.crosswalk)
    elif has_cusip and not args.fts_cache:
        n = sum(1 for r in rows if r["cusip6"] and not r["cik"])
        print(f"WARNING: {n} row(s) carry a CUSIP but no --crosswalk was given; "
              f"resolving via CIK/ticker only (pass --crosswalk to use CUSIP6->CIK "
              f"and to cross-check tickers).", file=sys.stderr)
    if args.fts_cache and Path(args.fts_cache).exists():
        for c6, ciks in load_cusip_crosswalk(args.fts_cache).items():
            crosswalk.setdefault(c6, set()).update(ciks)

    ticker_table = load_company_tickers(fetcher) if any(r["ticker"] for r in rows) else {}
    fts = EdgarFTS(fetcher) if args.fts else None
    has_name = any(r["name"] for r in rows)
    name_index = name_cache = None
    ledger_path = args.name_cache or str(cfg.name_cache_path)
    if has_name and not args.no_name_resolution:
        name_index, name_cache, ledger_path = _name_tier(args, cfg, fetcher)
    issuers, collisions, unresolved = reconcile_identifiers(
        rows, ticker_table, crosswalk, fts=fts, fts_limit=args.fts_limit,
        name_index=name_index, name_cache=name_cache)
    if name_index is not None:
        pairs = [(i.company, i.cik) for i in issuers
                 if i.resolution == "name" and i.cik and i.company]
        if pairs:
            total = write_name_cache(ledger_path, pairs)
            print(f"name-cache: merged {len(pairs)} name->CIK pin(s) -> "
                  f"{ledger_path} ({total} total)", file=sys.stderr)

    if not args.drop_collisions:
        for c in collisions:
            cik = c["cik_ticker"] if args.prefer == "ticker" else c["cik_cusip"]
            issuers.append(Issuer(cik=cik, ticker=c["ticker"], company=c["name"],
                                  cusip6=c["cusip6"], resolution=f"collision:{c['kind']}:{args.prefer}"))

    by_kind = Counter(c["kind"] for c in collisions)
    fts_conf = sum(1 for i in issuers if i.resolution == "fts:confirmed")
    fts_unv = sum(1 for i in issuers if i.resolution == "fts:unverified")
    print(f"resolved {len(issuers)} issuers; {len(collisions)} collision(s) "
          f"[{by_kind.get('name_mismatch', 0)} name_mismatch, {by_kind.get('name_match', 0)} name_match]; "
          f"fts {fts_conf} confirmed / {fts_unv} unverified; "
          f"{len(unresolved)} unresolved (of {len(rows)} input names)", file=sys.stderr)

    if args.fts_cache:
        pairs = [(i.cik, i.cusip6) for i in issuers
                 if i.resolution == "fts:confirmed" and i.cik and i.cusip6]
        if pairs:
            total = write_cusip_crosswalk(args.fts_cache, pairs)
            print(f"fts-cache: merged {len(pairs)} confirmed pair(s) -> {args.fts_cache} "
                  f"({total} total)", file=sys.stderr)

    if args.write:
        uni = Universe(cfg)
        path = uni.save(args.name, issuers)
        msg = f"wrote {len(issuers)} issuers -> {path}"
        if collisions:
            cpath = uni.path(args.name).with_name(f"{args.name}_collisions.jsonl")
            with cpath.open("w", encoding="utf-8") as fh:
                for c in collisions:
                    fh.write(json.dumps(c, ensure_ascii=False) + "\n")
            msg += f"; {len(collisions)} collisions -> {cpath}"
        print(msg)
    else:
        print(f"[dry-run] {len(issuers)} issuers for universe '{args.name}' "
              f"({len(collisions)} collisions held out). Re-run with --write to persist.")
    return 0


def _cmd_list_universe(args: argparse.Namespace) -> int:
    uni = Universe(_config(args))
    if not args.name:
        names = uni.names()
        print("universes:", ", ".join(names) if names else "(none)")
        return 0
    issuers = uni.load(args.name)
    print(f"universe '{args.name}': {len(issuers)} issuers")
    for it in issuers:
        print(f"  {it.cik}  {it.ticker:<8} {it.company}")
    return 0


def _cmd_discover(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ciks = _ciks_for(args, cfg)
    scope = parse_scope(args.forms)
    year_min, year_max, since, until = _period_args(args)
    # Discovery takes a single lower-bound date: prefer an explicit --since, else
    # the --years lower bound, else the documented last-20-years default.
    disc_since = since or date(min(_parse_years(args.years)), 1, 1)
    dry_run = not args.write

    # --download implies persisting the manifest (records must exist on disk).
    if args.download:
        dry_run = False

    report = discover_universe(
        ciks,
        scope=scope,
        since=disc_since,
        dry_run=dry_run,
        max_rounds=args.rounds,
        config=cfg,
        run_id=_run_id(args),
    )
    mode = "DRY-RUN (nothing written)" if dry_run else "WROTE manifests"
    s = report.stats
    print(f"discover [{mode}] — {report.issuers} issuers, {report.rounds} round(s)")
    print(f"  seen={s.seen} added={s.added} updated={s.updated} unchanged={s.unchanged}")
    if report.errors:
        print(f"  discovery errors: {len(report.errors)} (see discovery_errors.jsonl)")
    _feed_report(getattr(args, "report", None), "sec", seen=s.seen, new=s.added,
                 failed=len(report.errors), errors=report.errors)

    if args.download:
        # Use the same period filter as the standalone `download` command: bounds
        # are applied only when --years/--since/--until were given (no implicit
        # 20-year cap that would silently skip in-scope filings).
        dl = download_universe(
            ciks, scope=scope, year_min=year_min, year_max=year_max,
            since=since, until=until,
            dry_run=False, overwrite=args.overwrite, limit=args.limit, config=cfg,
            run_id=_run_id(args),
        )
        print(f"download — got={dl.downloaded} skipped={dl.skipped} errors={dl.errors} "
              f"bytes={dl.bytes:,}")
        # The download leg folds into the same `sec` source: both legs of a
        # `discover --download` run are work on SEC documents.
        _feed_report(getattr(args, "report", None), "sec",
                     seen=dl.downloaded + dl.skipped + dl.empty + dl.errors,
                     new=dl.downloaded, failed=dl.errors, errors=dl.error_items)
    return 0


def _cmd_download(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ciks = _ciks_for(args, cfg)
    scope = parse_scope(args.forms)
    year_min, year_max, since, until = _period_args(args)
    dry_run = not args.write
    dl = download_universe(
        ciks, scope=scope, year_min=year_min, year_max=year_max, since=since, until=until,
        dry_run=dry_run, overwrite=args.overwrite, limit=args.limit, config=cfg,
        run_id=_run_id(args),
    )
    mode = "DRY-RUN (nothing written)" if dry_run else "WROTE"
    print(f"download [{mode}] — got={dl.downloaded} skipped={dl.skipped} "
          f"empty={dl.empty} errors={dl.errors} bytes={dl.bytes:,}")
    if dl.error_items:
        print(f"  errors logged: {len(dl.error_items)} (see discovery_errors.jsonl)")
    # No would-download counter exists, so a dry run legitimately reports
    # docs_new=0 (documented in the module docstring).
    _feed_report(getattr(args, "report", None), "sec",
                 seen=dl.downloaded + dl.skipped + dl.empty + dl.errors,
                 new=dl.downloaded, failed=dl.errors, errors=dl.error_items)
    return 0


def _cmd_render_pdf(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ciks = _ciks_for(args, cfg)
    scope = parse_scope(args.forms)
    year_min, year_max, since, until = _period_args(args)
    dry_run = not args.write
    try:
        rep = render_universe(
            ciks, scope=scope, year_min=year_min, year_max=year_max, since=since, until=until,
            dry_run=dry_run, overwrite=args.overwrite, limit=args.limit, config=cfg,
            run_id=_run_id(args),
        )
    except RuntimeError as exc:  # e.g. Chrome not installed
        print(f"render-pdf: {exc}", file=sys.stderr)
        return 1
    mode = "DRY-RUN (nothing written)" if dry_run else "WROTE"
    print(f"render-pdf [{mode}] — rendered={rep.rendered} would-render={rep.would_render} "
          f"skipped={rep.skipped} no-primary={rep.no_primary} errors={rep.errors}")
    if rep.error_items:
        print(f"  errors logged: {len(rep.error_items)} (see discovery_errors.jsonl)")
    # rendered and would_render are mutually exclusive per record: summing them
    # makes a dry run count what it WOULD render as the useful work.
    _feed_report(getattr(args, "report", None), "sec",
                 seen=rep.rendered + rep.would_render + rep.skipped
                 + rep.no_primary + rep.errors,
                 new=rep.rendered + rep.would_render,
                 failed=rep.errors, errors=rep.error_items)
    return 0


def _cmd_xbrl(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ciks = _ciks_for(args, cfg)
    years = _parse_years(args.years) if args.years else None
    since_year = min(years) if years else None
    until_year = max(years) if years else None
    dry_run = not args.write
    rep = fetch_financials(ciks, since_year=since_year, until_year=until_year,
                           dry_run=dry_run, config=cfg, run_id=_run_id(args))
    mode = "DRY-RUN (nothing written)" if dry_run else "WROTE"
    s = rep.stats
    print(f"xbrl [{mode}] — {rep.issuers} issuers, {rep.periods} period summaries (F1)")
    print(f"  seen={s.seen} added={s.added} updated={s.updated} unchanged={s.unchanged}")
    if rep.errors:
        print(f"  errors: {len(rep.errors)} (see discovery_errors.jsonl)")
    # The unit of work is the period summary (computed in dry-run too), not the
    # manifest record: `rep.stats.added` is 0 on a re-run that still produced them.
    _feed_report(getattr(args, "report", None), "sec", seen=rep.issuers,
                 new=rep.periods, failed=len(rep.errors), errors=rep.errors)
    return 0


def _eu_specs(args: argparse.Namespace) -> list[dict]:
    if getattr(args, "leis", None):
        return [{"lei": x.strip()} for x in args.leis.split(",") if x.strip()]
    if getattr(args, "isins", None):
        return [{"isin": x.strip()} for x in args.isins.split(",") if x.strip()]
    return []


def _spec_label(spec: dict) -> str:
    """``{"isin": "FR…"}`` -> ``"ISIN FR…"`` (the identifier the caller typed)."""
    for key in ("lei", "isin", "orgnr", "name", "ticker"):
        if spec.get(key):
            return f"{key.upper()} {spec[key]}"
    return repr(spec)


def _unresolved_verdict(cmd: str, out: dict) -> int:
    """Print the specs that resolved to no identity; fail when NONE resolved.

    An unresolved spec reaches no source and no report row, so its only trace
    is the ``unresolved: …`` stdout line. When every spec is unresolved nothing
    was queried at all and the run would pass for a green nothing-to-do — it
    returns 2 instead (folded into a ``failed`` report by :func:`main`), and the
    message names GLEIF as a possible cause, since an unreachable resolver
    looks exactly like a bad identifier. Returns 0 otherwise.
    """
    unresolved_specs = out.get("unresolved_specs") or []
    if unresolved_specs:
        print("  unresolved: " + ", ".join(_spec_label(s) for s in unresolved_specs))
    entities = int(out.get("entities") or 0)
    unresolved = int(out.get("unresolved") or 0)
    if entities and unresolved >= entities:
        print(f"error: {cmd}: every spec is unresolved ({entities} entities, no identity: "
              "bad identifiers, or GLEIF unreachable) — no source was queried",
              file=sys.stderr)
        return 2
    return 0


def _cmd_eu_financials(args: argparse.Namespace) -> int:
    """Build IFRS financials from ESEF; reported under ``xbrlorg``.

    The rows this command writes carry ``source="esef"`` and come from the
    filings.xbrl.org aggregator, not from a national OAM backend — so the
    aggregator is the authority on the hook for them. Specs that resolved to
    no LEI are printed, and a run in which none resolved fails (see
    :func:`_unresolved_verdict`): dead GLEIF must not read as "no financials".
    """
    cfg = _config(args)
    fetcher = Fetcher(cfg)
    rep = build_eu_financials(_eu_specs(args), fetcher=fetcher, config=cfg, write=args.write,
                              use_arelle=args.arelle)
    mode = "WROTE" if args.write else "DRY-RUN (nothing written)"
    print(f"eu-financials [{mode}] — {rep['entities']} entities, "
          f"{rep['with_financials']} with financials, {rep['periods']} period summaries")
    if rep.get("coverage_path"):
        print(f"  coverage: {rep['coverage_path']}")
    _feed_from_out(getattr(args, "report", None), "esef", rep)
    _record_out_errors(args, cfg, rep)
    return _unresolved_verdict("eu-financials", rep)


def _cmd_eu_acquire(args: argparse.Namespace) -> int:
    """Discover (and with ``--write``, download) an EU issuer's regulated filings.

    Dry run by default: discovery only, nothing on disk (no entity index, no
    coverage file, no manifest, no trail) — the run report still counts the
    documents that WOULD be acquired and any dead backend. ``--write`` downloads
    and writes everything; ``--write --no-download`` is a discovery-only run
    that still leaves the entity index, the coverage file and the error trail.

    The run report gets one row per backend (see the module docstring). A spec
    that resolved to no LEI reaches no backend and therefore no report row, so
    it is printed (``unresolved: ISIN …, LEI …``) — that stdout line is its only
    trace on a dry run. A run whose specs ALL failed to resolve reached no
    backend at all, so nothing would report and the run would pass for a green
    nothing-to-do: it returns 2 instead, which :func:`main` folds into a
    ``failed`` report — and the message says the cause may be GLEIF itself,
    since an unreachable resolver looks exactly like a bad input.
    """
    cfg = _config(args)
    fetcher = Fetcher(cfg)
    download = bool(args.write and not args.no_download)
    out = acquire(_eu_specs(args), fetcher=fetcher, config=cfg,
                  download=download, write=args.write)
    if not args.write:
        mode = "DRY-RUN (nothing written)"
        print("note: --no-download is implied without --write")
    elif download:
        mode = "WROTE"
    else:
        mode = "WROTE (discovery only, no download)"
    verb = "acquired" if download else "would acquire"
    print(f"eu-acquire [{mode}] — {out['entities']} entities "
          f"({out.get('unresolved', 0)} unresolved), {verb} {out['documents']} documents, "
          f"{out['manifests']} manifests, {out['deduped_by_bytes']} byte-deduped, "
          f"{out.get('documents_failed', 0)} documents failed, "
          f"{out['download_errors']} download errors, {len(out['errors'])} errors")
    sources = out.get("sources") or {}
    for name in sorted(sources):
        st = sources[name]
        print(f"  {name}: entities={st['entities']} documents={st['documents']} "
              f"errors={st['errors']} not-indexed={st.get('not_indexed', 0)}"
              f"{' TRUNCATED' if st.get('truncated') else ''}")
    if out.get("coverage_path"):
        print(f"  coverage: {out['coverage_path']}")

    # The trail first: it must survive a report-feeding failure below.
    _record_out_errors(args, cfg, out)
    report = getattr(args, "report", None)
    items_by_source: dict[str, list] = {}
    for item in out.get("error_items") or []:
        key = item.get("source") if isinstance(item, dict) else None
        items_by_source.setdefault(key, []).append(item)
    for name, st in sources.items():
        _feed_report(report, name, seen=st["entities"], new=st["documents"],
                     failed=st["errors"], errors=items_by_source.pop(name, []),
                     truncated=bool(st.get("truncated", False)))
    # An error tagged with a backend that reported no counts (cannot happen by
    # construction) is still fed under that backend's code — never dropped; an
    # untagged one is a producer bug and raises rather than vanish.
    for name, items in items_by_source.items():
        if not name:
            raise RuntimeError(f"eu-acquire: {len(items)} error item(s) carry no backend "
                               f"source: {items[0]!r}")
        _feed_report(report, name, failed=len(items), errors=items)
    return _unresolved_verdict("eu-acquire", out)


def _register_specs(args: argparse.Namespace) -> list[dict]:
    if getattr(args, "orgnrs", None):
        return [{"orgnr": x.strip()} for x in args.orgnrs.split(",") if x.strip()]
    if getattr(args, "leis", None):
        return [{"lei": x.strip()} for x in args.leis.split(",") if x.strip()]
    return []


def _print_reg_result(rep: dict, args: argparse.Namespace) -> None:
    """Print the standard register-financials result line (+ coverage path).

    Shared by every register source except the Norway/LEI fallback, whose line
    historically omits the unbalanced count and is printed inline.
    """
    mode = "WROTE" if args.write else "DRY-RUN (nothing written)"
    print(f"register-financials [{mode}] — {rep['entities']} entities, "
          f"{rep['with_financials']} with financials, "
          f"{rep.get('unbalanced', 0)} unbalanced, "
          f"{rep['periods']} period summaries")
    if rep.get("coverage_path"):
        print(f"  coverage: {rep['coverage_path']}")


def _cmd_register_financials(args: argparse.Namespace) -> int:
    cfg = _config(args)
    if getattr(args, "limit", None) is not None and not (
        getattr(args, "ch_bulk", None)
        or getattr(args, "ee_file", None)
        or getattr(args, "ee_year", None)
        or getattr(args, "sk_id", None)
    ):
        raise SystemExit("error: --limit requires --ch-bulk, --ee-file, --ee-year, or --sk-id")

    # Register-source dispatch, checked in order: the FIRST flag present wins
    # (identical precedence to the former if/elif chain).  Each entry maps an
    # args attribute to the producer's own `source` tag (resolved to the
    # authority's code for the run report) and a zero-arg builder returning the
    # `rep` dict; the shared result line is printed via _print_reg_result.
    # Denmark is the one entry carrying a canonical code rather than a producer
    # tag: its rows are tagged `erst-fsa` or `erst-ifrs` by *document format*,
    # while the authority (Erhvervsstyrelsen) is the same either way.
    # Builders instantiate Fetcher lazily so only the selected path opens a
    # session.  The Norway/LEI path below is the default fallback (its result
    # line omits the unbalanced count, so it is printed inline rather than via
    # _print_reg_result).
    def _build_be_numbers() -> dict:
        be_key = os.environ.get("BNB_CBSO_KEY") or ""
        if not be_key:
            raise SystemExit(
                "error: --be-numbers requires a CBSO API key — "
                "set the BNB_CBSO_KEY environment variable"
            )
        return build_be_financials(
            [{"be_number": k} for k in args.be_numbers],
            fetcher=Fetcher(cfg), config=cfg, key=be_key, write=args.write)

    dispatch = [
        # Estonia keyless path: elements CSV + metadata CSV (local files or bytes)
        ("ee_file", "rik", lambda: build_ee_financials_from_files(
            *args.ee_file, config=cfg, write=args.write,
            limit=getattr(args, "limit", None))),
        # Estonia online path: keyless bulk download for a given year
        ("ee_year", "rik", lambda: build_ee_financials(
            args.ee_year, fetcher=Fetcher(cfg), config=cfg, write=args.write,
            limit=getattr(args, "limit", None),
            elem_url=getattr(args, "ee_elem_url", None),
            meta_url=getattr(args, "ee_meta_url", None))),
        # Finland keyless path: one or more local PRH XBRL .xml files
        ("fi_file", "prh", lambda: build_fi_financials_from_files(
            args.fi_file, config=cfg, write=args.write)),
        # Finland API path: Y-tunnus resolved via PRH XBRL API (keyless)
        ("fi_businessid", "prh", lambda: build_fi_financials(
            [{"business_id": bid} for bid in args.fi_businessid],
            fetcher=Fetcher(cfg), config=cfg, write=args.write)),
        # Belgium keyless path: one or more local .xbrl / .zip files
        ("be_file", "bnb", lambda: build_be_financials_from_files(
            args.be_file, config=cfg, write=args.write)),
        # Belgium API path: KBO numbers resolved via the CBSO API (needs key)
        ("be_numbers", "bnb", _build_be_numbers),
        # UK Companies House bulk path
        ("ch_bulk", "companies_house", lambda: build_ch_financials(
            args.ch_bulk, config=cfg, write=args.write,
            limit=getattr(args, "limit", None))),
        # Luxembourg keyless path: one or more local eCDF XML files
        ("lu_file", "lbr", lambda: build_lu_financials_from_files(
            args.lu_file, config=cfg, write=args.write,
            rcs_filter=set(args.rcs) if getattr(args, "rcs", None) else None)),
        # Denmark keyless path: one or more local Virk XBRL .xml files
        ("dk_file", "erst", lambda: build_dk_financials_from_files(
            args.dk_file, config=cfg, write=args.write)),
        # Denmark API path: CVR numbers resolved via Virk Regnskaber (keyless)
        ("dk_cvr", "erst", lambda: build_dk_financials(
            [{"cvr": c} for c in args.dk_cvr],
            fetcher=Fetcher(cfg), config=cfg, write=args.write)),
        # Slovakia keyless path: one vykaz JSON + one sablona JSON (local files)
        ("sk_file", "registeruz", lambda: build_sk_financials_from_files(
            *args.sk_file, config=cfg, write=args.write)),
        # Slovakia API traverse path: entity IDs → registeruz.sk (keyless)
        ("sk_id", "registeruz", lambda: build_sk_financials(
            args.sk_id, fetcher=Fetcher(cfg), config=cfg, write=args.write,
            limit=getattr(args, "limit", None))),
    ]
    for attr, source_tag, builder in dispatch:
        if getattr(args, attr, None):
            out = builder()
            _print_reg_result(out, args)
            _feed_from_out(getattr(args, "report", None), source_tag, out)
            _record_out_errors(args, cfg, out)
            return 0

    # --- Norway / LEI path ---
    # A LEI spec resolves through GLEIF; dead GLEIF must not read as "no
    # financials", so the unresolved specs are printed and a run in which
    # none resolved fails (see _unresolved_verdict).
    rep = build_register_financials(
        _register_specs(args), fetcher=Fetcher(cfg), config=cfg, write=args.write)
    mode = "WROTE" if args.write else "DRY-RUN (nothing written)"
    print(f"register-financials [{mode}] — {rep['entities']} entities, "
          f"{rep['with_financials']} with financials, {rep['periods']} period summaries")
    if rep.get("coverage_path"):
        print(f"  coverage: {rep['coverage_path']}")
    _feed_from_out(getattr(args, "report", None), "brreg", rep)
    _record_out_errors(args, cfg, rep)
    return _unresolved_verdict("register-financials", rep)


def _cmd_ownership(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ciks = _ciks_for(args, cfg)
    scope = parse_scope(args.forms) if args.forms else None
    year_min, year_max, since, until = _period_args(args)
    dry_run = not args.write
    rep = process_ownership(ciks, scope=scope, year_min=year_min, year_max=year_max,
                            since=since, until=until, dry_run=dry_run,
                            overwrite=args.overwrite, limit=args.limit, config=cfg,
                            run_id=_run_id(args))
    mode = "DRY-RUN (nothing written)" if dry_run else "WROTE"
    print(f"ownership [{mode}] — {rep.issuers} issuers, downloaded={rep.downloaded} "
          f"would-download={rep.would_download}")
    print(f"  insider(E1)={rep.parsed_insider} 13F(E2)={rep.parsed_13f} "
          f"narrative(E3)={rep.passthrough} errors={rep.errors}")
    if rep.error_items:
        print(f"  errors logged: {len(rep.error_items)} (see discovery_errors.jsonl)")
    # Parsed rows and would-download are mutually exclusive per record (a dry
    # run parses nothing): summing them makes a dry run count what it WOULD
    # download as the useful work, like render-pdf.
    _feed_report(getattr(args, "report", None), "sec", seen=rep.issuers,
                 new=rep.parsed_insider + rep.parsed_13f + rep.passthrough
                 + rep.would_download,
                 failed=rep.errors, errors=rep.error_items)
    return 0


def _cmd_rag_items(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ciks = None
    if args.universe:
        ciks = list(Universe(cfg).iter_ciks(args.universe))
    elif args.ciks:
        ciks = [c for c in args.ciks.split(",") if c.strip()]
    items = iter_items(
        ciks=ciks, doctypes=args.forms, year_min=args.year_min,
        year_max=args.year_max, prefer=args.prefer, config=cfg,
    )
    n = 0
    for it in items:
        n += 1
        if n <= args.show:
            p = it.payload
            print(f"{it.doc_id}  {p['doc_type']:<3} {p.get('year')}  {p['company']}  -> {it.path}")
    print(f"rag-items: {n} ingestible item(s) [prefer={args.prefer}]")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    cfg = _config(args)
    ciks = _ciks_for(args, cfg)
    scope = parse_scope(args.forms)
    years = _parse_years(args.years)
    rows = build_matrix(ciks, years, scope, storage=Storage(cfg), config=cfg)

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {len(rows)} matrix rows -> {args.csv}")
    tally = summarize(rows)
    print("completeness:", ", ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "(empty)")
    return 0


def _cmd_entities(args: argparse.Namespace) -> int:
    reg = EntityRegistry(_config(args)).load()
    if args.cik:
        ent = reg.resolve(args.cik)
        if ent:
            print(f"{normalize_cik(args.cik)} -> entity '{ent.entity_id}' ({ent.name})")
            print(f"  CIKs: {', '.join(ent.ciks)}")
            if ent.note:
                print(f"  note: {ent.note}")
        else:
            print(f"{normalize_cik(args.cik)} -> standalone (no alias entry)")
        return 0
    ents = list(reg.entities())
    print(f"entities: {len(ents)}")
    for e in ents:
        print(f"  {e.entity_id:<16} {e.name}  [{', '.join(e.ciks)}]")
    return 0


def _cmd_discover_index(args: argparse.Namespace) -> int:
    cfg = _config(args)
    scope = parse_scope(args.forms)
    years = _parse_years(args.years)
    reg = EntityRegistry(cfg).load()

    cik_filter: set[str] | None = None
    if args.universe:
        cik_filter = set(reg.expand_all(Universe(cfg).iter_ciks(args.universe)))
    elif args.ciks:
        cik_filter = set(reg.expand_all(c for c in args.ciks.split(",") if c.strip()))
    elif not args.all:
        raise SystemExit("error: provide --universe, --ciks, or --all (full EDGAR)")

    dry_run = not args.write
    storage = Storage(cfg)
    src = EdgarFullIndex(fetcher=Fetcher(cfg), config=cfg)
    from .storage import SaveStats

    stats = SaveStats()
    for year in years:
        for quarter in (1, 2, 3, 4):
            recs = list(src.discover(year, quarter, scope=scope, ciks=cik_filter))
            for rec in recs:
                eid = reg.entity_id_for(rec.cik)
                if eid:
                    rec.entity_id = eid
            stats += storage.save_records(recs, dry_run=dry_run)
    if src.errors and not dry_run:
        storage.record_errors(src.errors, run_id=_run_id(args))

    mode = "DRY-RUN (nothing written)" if dry_run else "WROTE manifests"
    span = f"{years[0]}-{years[-1]}" if years else "(none)"
    scope_note = "ALL filers" if cik_filter is None else f"{len(cik_filter)} CIKs"
    print(f"discover-index [{mode}] — years={span}, {scope_note}")
    print(f"  seen={stats.seen} added={stats.added} updated={stats.updated} unchanged={stats.unchanged}")
    if src.errors:
        print(f"  index errors: {len(src.errors)} (see discovery_errors.jsonl)")
    _feed_report(getattr(args, "report", None), "sec", seen=stats.seen,
                 new=stats.added, failed=len(src.errors), errors=src.errors)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="company-corpus")
    p.add_argument("--version", action="version", version=f"company-corpus {__version__}")
    p.add_argument("--data-dir", default=None, dest="data_dir",
                   help="corpus root holding manifest/, raw/, … (default: ./data)")
    p.add_argument("--contact", default=None,
                   help="contact for the SEC User-Agent (overrides $COMPANY_CORPUS_CONTACT)")
    p.add_argument("--insecure", action="store_true",
                   help="disable TLS certificate verification (only behind a trusted SSL-inspection proxy)")
    sub = p.add_subparsers(dest="cmd", required=True)

    lf = sub.add_parser("list-forms", help="show the filing taxonomy (families A-F)")
    lf.add_argument("--forms", default="all", help="code/family selector, e.g. A,B or A1,B1")
    lf.set_defaults(func=_cmd_list_forms)

    cf = sub.add_parser("config", help="print the effective runtime configuration")
    cf.set_defaults(func=_cmd_config)

    bu = sub.add_parser("build-universe", help="resolve tickers/CIKs/index -> a curated list")
    bu.add_argument("--tickers", default="", help="comma-separated tickers, e.g. AAPL,MSFT")
    bu.add_argument("--ciks", default="", help="comma-separated CIKs (for delisted/historical issuers)")
    bu.add_argument("--from-file", default=None,
                    help="CSV of identifiers (auto-detects CIK/Ticker/CUSIP/ISIN columns)")
    bu.add_argument("--cik-col", default=None, help="override the CIK column name in --from-file")
    bu.add_argument("--ticker-col", default=None, help="override the ticker column name in --from-file")
    bu.add_argument("--cusip-col", default=None, help="override the CUSIP/ISIN column name in --from-file")
    bu.add_argument("--crosswalk", default=None,
                    help="CUSIP6->CIK crosswalk CSV (cik,cusip6,cusip8) for --from-file resolution")
    bu.add_argument("--drop-collisions", action="store_true",
                    help="exclude ticker/CUSIP6 collisions from the universe (default: keep them)")
    bu.add_argument("--prefer", choices=["ticker", "cusip"], default="cusip",
                    help="which CIK to trust for kept collisions (default: cusip, issuer-anchored)")
    bu.add_argument("--fts", action="store_true",
                    help="resolve still-unresolved rows via EDGAR full-text search (network, opt-in)")
    bu.add_argument("--fts-limit", type=int, default=None,
                    help="cap the number of --fts lookups (for bounded runs)")
    bu.add_argument("--fts-cache", default=None,
                    help="CSV cache of CUSIP6->CIK (read to skip EFTS; fts:confirmed hits appended). "
                         "Written whenever given, independent of --write.")
    bu.add_argument("--no-name-resolution", action="store_true",
                    help="disable the name->CIK tier (default: on for name-bearing universes)")
    bu.add_argument("--name-cache", default=None,
                    help="name->CIK ledger CSV (default: data/reference/name_cik_cache.csv); "
                         "written whenever the name tier runs, independent of --write")
    bu.add_argument("--equity-index", choices=["sp500"], dest="equity_index",
                    help="build from an equity index's composition (fetched by name)")
    bu.add_argument("--index", choices=["sp500"], dest="index_legacy",
                    help=argparse.SUPPRESS)  # deprecated alias for --equity-index
    bu.add_argument("--since", default=None, help="with --equity-index: window start (YYYY) for the historical union")
    bu.add_argument("--current-only", action="store_true", help="with --equity-index: today's members only (no history)")
    bu.add_argument("--name", default="curated", help="universe name (file stem; defaults to index name)")
    bu.add_argument("--write", action="store_true", help="persist to data/universe/<name>.jsonl")
    bu.set_defaults(func=_cmd_build_universe)

    en = sub.add_parser("entities", help="list cross-CIK entities or resolve a CIK's entity")
    en.add_argument("--cik", default="", help="resolve a single CIK to its entity")
    en.set_defaults(func=_cmd_entities)

    dx = sub.add_parser("discover-index", help="exhaustive discovery via quarterly full-index (incl. delisted)")
    dxsrc = dx.add_mutually_exclusive_group(required=False)
    dxsrc.add_argument("--universe", help="restrict to a universe's CIKs (alias-expanded)")
    dxsrc.add_argument("--ciks", help="restrict to comma-separated CIKs (alias-expanded)")
    dx.add_argument("--all", action="store_true", help="index ALL filers (no CIK filter; very large)")
    dx.add_argument("--forms", default=None, help="scope selector (default: narrative A-D)")
    dx.add_argument("--years", default=None, help="year range, e.g. 2006-2025 (default: last 20)")
    dx.add_argument("--write", action="store_true", help="persist manifests (else dry-run)")
    dx.set_defaults(func=_cmd_discover_index)

    lu = sub.add_parser("list-universe", help="list curated universes or one universe's issuers")
    lu.add_argument("--name", default="", help="universe name; omit to list all")
    lu.set_defaults(func=_cmd_list_universe)

    di = sub.add_parser("discover", help="discover filings into manifests (dry-run by default)")
    src = di.add_mutually_exclusive_group(required=True)
    src.add_argument("--universe", help="universe name to crawl")
    src.add_argument("--ciks", help="comma-separated CIKs to crawl")
    di.add_argument("--forms", default=None, help="scope selector (default: narrative A-D)")
    di.add_argument("--years", default=None, help="year filter, e.g. 2006-2025 (default: last 20)")
    di.add_argument("--since", default=None, help="start date for the --download step (YYYY-MM-DD)")
    di.add_argument("--until", default=None, help="end date for the --download step (YYYY-MM-DD)")
    di.add_argument("--rounds", type=int, default=1, help="max convergence rounds")
    di.add_argument("--write", action="store_true", help="persist manifests (else dry-run)")
    di.add_argument("--download", action="store_true", help="also download+decompose (implies --write)")
    di.add_argument("--overwrite", action="store_true", help="re-download already-stored filings")
    di.add_argument("--limit", type=int, default=None, help="cap number of new downloads")
    di.set_defaults(func=_cmd_discover)

    dl = sub.add_parser("download", help="download+decompose filings from existing manifests")
    dlsrc = dl.add_mutually_exclusive_group(required=True)
    dlsrc.add_argument("--universe", help="universe name")
    dlsrc.add_argument("--ciks", help="comma-separated CIKs")
    dl.add_argument("--forms", default=None, help="scope selector (default: narrative A-D)")
    _add_period_flags(dl)
    dl.add_argument("--write", action="store_true", help="persist files+manifest (else dry-run)")
    dl.add_argument("--overwrite", action="store_true", help="re-download already-stored filings")
    dl.add_argument("--limit", type=int, default=None, help="cap number of new downloads")
    dl.set_defaults(func=_cmd_download)

    rp = sub.add_parser("report", help="completeness matrix (issuer x form x year)")
    rsrc = rp.add_mutually_exclusive_group(required=True)
    rsrc.add_argument("--universe", help="universe name")
    rsrc.add_argument("--ciks", help="comma-separated CIKs")
    rp.add_argument("--forms", default=None, help="scope selector (default: narrative A-D)")
    rp.add_argument("--years", default=None, help="year range, e.g. 2006-2025 (default: last 20)")
    rp.add_argument("--csv", default="", help="optional CSV output path")
    rp.set_defaults(func=_cmd_report)

    rd = sub.add_parser("render-pdf", help="render downloaded primary docs to PDF (separate batch)")
    rdsrc = rd.add_mutually_exclusive_group(required=True)
    rdsrc.add_argument("--universe", help="universe name")
    rdsrc.add_argument("--ciks", help="comma-separated CIKs")
    rd.add_argument("--forms", default=None, help="scope selector (default: narrative A-D)")
    _add_period_flags(rd)
    rd.add_argument("--write", action="store_true", help="render+persist (else dry-run)")
    rd.add_argument("--overwrite", action="store_true", help="re-render already-rendered filings")
    rd.add_argument("--limit", type=int, default=None, help="cap number of new renders")
    rd.set_defaults(func=_cmd_render_pdf)

    ef = sub.add_parser("enrich-openfigi",
                        help="enrich identifiers (ISIN/CUSIP) via OpenFIGI: name/type/exchange + triage")
    ef.add_argument("--from-file", required=True, help="CSV carrying an ISIN or CUSIP column")
    ef.add_argument("--id-col", default=None, help="identifier column name (default: the --id-type column)")
    ef.add_argument("--id-type", choices=["isin", "cusip"], default="isin")
    ef.add_argument("--api-key", default=None,
                    help="OpenFIGI API key (optional; else $OPENFIGI_API_KEY; raises rate limits)")
    ef.add_argument("--out", default=None, help="output CSV path")
    ef.set_defaults(func=_cmd_enrich_openfigi)

    xb = sub.add_parser("xbrl", help="build per-period XBRL financial summaries (family F1)")
    xbsrc = xb.add_mutually_exclusive_group(required=True)
    xbsrc.add_argument("--universe", help="universe name")
    xbsrc.add_argument("--ciks", help="comma-separated CIKs")
    xb.add_argument("--years", default=None,
                    help="keep periods whose fiscal year is in this range, e.g. 2015-2025 or 2024")
    xb.add_argument("--write", action="store_true", help="persist summaries+facts (else dry-run)")
    xb.set_defaults(func=_cmd_xbrl)

    euf = sub.add_parser("eu-financials",
                         help="build per-period IFRS financial summaries from ESEF (filings.xbrl.org)")
    eufsrc = euf.add_mutually_exclusive_group(required=True)
    eufsrc.add_argument("--leis", help="comma-separated LEIs")
    eufsrc.add_argument("--isins", help="comma-separated ISINs")
    euf.add_argument("--write", action="store_true", help="persist tables (else dry-run)")
    euf.add_argument("--arelle", action="store_true",
                     help="also parse local ESEF .zip packages with Arelle (Tier B; needs the eu-financials extra)")
    euf.set_defaults(func=_cmd_eu_financials)

    eua = sub.add_parser("eu-acquire",
                         help="discover (and with --write, download) an EU issuer's regulated "
                              "filings from its national OAM + filings.xbrl.org (+ Euronext)")
    euasrc = eua.add_mutually_exclusive_group(required=True)
    euasrc.add_argument("--leis", help="comma-separated LEIs")
    euasrc.add_argument("--isins", help="comma-separated ISINs (resolved to LEIs via GLEIF)")
    eua.add_argument("--write", action="store_true",
                     help="download files and write manifests, the entity index, the coverage "
                          "file and the error trail (else dry-run: discovery only, nothing written)")
    eua.add_argument("--no-download", action="store_true", dest="no_download",
                     help="with --write: discovery only (entity index + coverage + trail, no files)")
    eua.set_defaults(func=_cmd_eu_acquire)

    rf = sub.add_parser("register-financials",
                        help="build financials from national business registers (statutory/private)")
    rfsrc = rf.add_mutually_exclusive_group(required=True)
    rfsrc.add_argument("--orgnrs", help="comma-separated Norwegian org numbers")
    rfsrc.add_argument("--leis", help="comma-separated LEIs (resolved to orgnr via GLEIF)")
    rfsrc.add_argument("--ch-bulk", metavar="ZIP", dest="ch_bulk",
                       help="UK Companies House Accounts Bulk Data .zip file (GB)")
    rfsrc.add_argument("--be-file", nargs="+", metavar="PATH", dest="be_file",
                       help="one or more BNB -data.xbrl or deposit .zip files (BE, keyless parse)")
    rfsrc.add_argument("--be-numbers", nargs="+", metavar="KBO", dest="be_numbers",
                       help="one or more KBO numbers (BE, CBSO API; key via $BNB_CBSO_KEY)")
    rfsrc.add_argument("--fi-file", nargs="+", metavar="PATH", dest="fi_file",
                       help="one or more PRH XBRL .xml files (FI, keyless local parse)")
    rfsrc.add_argument("--fi-businessid", nargs="+", metavar="Y_TUNNUS",
                       dest="fi_businessid",
                       help="one or more Finnish Y-tunnus values (FI, PRH open API, keyless)")
    rfsrc.add_argument("--lu-file", nargs="+", metavar="PATH", dest="lu_file",
                       help="one or more LBR eCDF XML files (LU, keyless parse)")
    rfsrc.add_argument("--dk-file", nargs="+", metavar="PATH", dest="dk_file",
                       help="one or more Virk XBRL .xml files (DK, keyless local parse, ESEF or FSA)")
    rfsrc.add_argument("--dk-cvr", nargs="+", metavar="CVR", dest="dk_cvr",
                       help="one or more Danish CVR numbers (DK, Virk Regnskaber API, keyless)")
    rfsrc.add_argument("--ee-file", nargs=2, metavar=("ELEM", "META"), dest="ee_file",
                       help="EE Äriregister: elements CSV/zip + metadata CSV/zip (EE, keyless)")
    rfsrc.add_argument("--ee-year", type=int, metavar="YYYY", dest="ee_year",
                       help="EE Äriregister: download bulk CSVs for this year (EE, keyless online)")
    rfsrc.add_argument("--sk-file", nargs=2, metavar=("VYKAZ", "SABLONA"), dest="sk_file",
                       help="SK registeruz.sk: vykaz JSON + sablona JSON (SK, keyless local parse)")
    rfsrc.add_argument("--sk-id", nargs="+", metavar="ENTITY_ID", dest="sk_id",
                       type=int,
                       help="one or more SK registeruz.sk entity IDs (SK, keyless traverse API)")
    rf.add_argument("--ee-elem-url", metavar="URL", dest="ee_elem_url", default=None,
                    help="explicit elements-zip URL (--ee-year; RIK snapshot date rotates)")
    rf.add_argument("--ee-meta-url", metavar="URL", dest="ee_meta_url", default=None,
                    help="explicit metadata-zip URL (--ee-year; RIK snapshot date rotates)")
    rf.add_argument("--rcs", nargs="+", metavar="RCS", dest="rcs",
                    help="filter to these RCS numbers (--lu-file only, e.g. B60814)")
    rf.add_argument("--limit", type=int, default=None,
                    help="cap number of entities/reports processed (--ch-bulk, --ee-file, --ee-year)")
    rf.add_argument("--write", action="store_true", help="persist tables (else dry-run)")
    rf.set_defaults(func=_cmd_register_financials)

    ow = sub.add_parser("ownership", help="download+structure ownership filings (family E)")
    owsrc = ow.add_mutually_exclusive_group(required=True)
    owsrc.add_argument("--universe", help="universe name (curated tier recommended)")
    owsrc.add_argument("--ciks", help="comma-separated CIKs")
    ow.add_argument("--forms", default="E", help="scope selector (default: E = E1,E2,E3)")
    _add_period_flags(ow)
    ow.add_argument("--write", action="store_true", help="download+persist summaries (else dry-run)")
    ow.add_argument("--overwrite", action="store_true", help="re-download already-stored filings")
    ow.add_argument("--limit", type=int, default=None, help="cap number of new downloads")
    ow.set_defaults(func=_cmd_ownership)

    ri = sub.add_parser("rag-items", help="preview SourceItems the RAG would ingest")
    risrc = ri.add_mutually_exclusive_group(required=False)
    risrc.add_argument("--universe", help="universe name")
    risrc.add_argument("--ciks", help="comma-separated CIKs (default: all manifests)")
    ri.add_argument("--forms", default=None, help="scope selector (default: all)")
    ri.add_argument("--year-min", type=int, default=None, dest="year_min")
    ri.add_argument("--year-max", type=int, default=None, dest="year_max")
    ri.add_argument("--prefer", choices=["pdf", "text", "primary"], default="pdf",
                    help="which stored artifact to feed (default: pdf)")
    ri.add_argument("--show", type=int, default=10, help="how many items to print")
    ri.set_defaults(func=_cmd_rag_items)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd not in REPORTING_CMDS:
        return args.func(args)

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )

    report = DoctrineReport("company-corpus", args.cmd)
    args.report = report
    try:
        func_rc = args.func(args)
    except Exception as exc:
        rc = report.finish(fatal=f"{type(exc).__name__}: {exc}")
        rc = _write_report(report, args, rc)
        print(f"error: {exc}", file=sys.stderr)
        return rc
    if isinstance(func_rc, int) and func_rc != 0:
        rc = report.finish(fatal=f"{args.cmd} returned exit code {func_rc}")
        rc = _write_report(report, args, rc)
        print(f"error: {args.cmd} returned exit code {func_rc}", file=sys.stderr)
        return rc
    rc = report.finish()
    return _write_report(report, args, rc)


def _write_report(report: DoctrineReport, args: argparse.Namespace, rc: int) -> int:
    """Write the run-report, surviving an unwritable runs path.

    A report that can't be persisted is itself a failed run for the doctrine:
    print an error and fold the exit code to at least 1 rather than let the
    OSError escape as a traceback.
    """
    path = str(_runs_path(args))
    try:
        report.write(path)
    except OSError as exc:
        print(f"error: could not write run report to {path}: {exc}", file=sys.stderr)
        return max(rc, 1)
    return rc
