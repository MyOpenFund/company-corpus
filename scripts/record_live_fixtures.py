"""Record real SEC responses for the live-mode parity tests (network; run by hand, never in CI).

Usage::

    MOF_SEC_CONTACT=<contact> python scripts/record_live_fixtures.py \\
        --ciks 789019 1652044 --since 2022-01-01 --quarters 2025q1 2025q3 \\
        --out tests/fixtures/live/recorded

The SEC contact (sent in the ``User-Agent``) is read from ``MOF_SEC_CONTACT`` only; the script
exits with status 2 when it is unset. It is never printed nor written: the fixtures hold response
bodies only, never request headers.

What is recorded, per issuer (``<out>/<cik>/``):

* ``companyfacts.json``: the companyfacts response, trimmed to the facts of the dictionary's tags
  (see ``--no-trim``). The trim is checked at recording time: the ``fundamentals`` rows built from
  the trimmed and the full file must be equal, else the full file is kept. The selection reads only
  those rows, so the trim cannot change a selected value;
* ``submissions/<name>``: the submissions main file and every history page, their filing
  columns trimmed to the accessions the trimmed companyfacts or the recorded filings name (a
  large filer lists tens of thousands of prospectuses). Checked like the companyfacts trim: the
  ``fundamentals`` and ``filing_statements`` rows must be equal with and without it, else the
  files are kept whole;
* ``<accession>/<file>``: what ``filing_positions`` fetches (``index.json``, ``FilingSummary.xml``,
  the presentation linkbase) for every filing of ``--forms`` whose report date is on or after
  ``--since`` and that the FSDS of ``--quarters`` lists (the two sides then cover the same filings);
* ``fsds.jsonl``: the FSDS rows of those accessions, built by ``opendata.statements`` itself
  (``_extract`` over the quarter zips), i.e. what the corpus would hold for them;
* ``manifest.json`` (at ``<out>``): every recorded URL -> file (``null`` for a 404), the
  accessions, the quarters, ``recorded_at`` (the ``now`` the tests use) and the trim outcomes.

Every body is stored gzip-compressed (``<file>.gz``, no timestamp in the gzip header), which
keeps the recorded set to a few MB.

The quarter zips (tens of MB) are downloaded to a temporary directory and deleted on exit.

``--find-amendments`` lists the 10-K/A of ``--quarters`` that carry primary statements in FSDS
(rows per statement), to pick one for the fixture set; nothing is written.

``--sections`` measures section detection on the newest 10-K primary document of each ``--ciks``
issuer (each conversion in a fresh process: time and peak RSS); nothing is written.
"""
from __future__ import annotations

import argparse
import gzip
import json
import multiprocessing
import os
import sys
import tempfile
import time
import zipfile
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from company_corpus.concepts.dictionary import load_dictionary  # noqa: E402
from company_corpus.config import Config, normalize_cik  # noqa: E402
from company_corpus.live.filings import FilingRef, fetch_submissions, list_filings  # noqa: E402
from company_corpus.live.issuer import POSITIONS_FORMS  # noqa: E402
from company_corpus.live.positions import filing_positions  # noqa: E402
from company_corpus.live.sec import SecClient, SecNotFound  # noqa: E402
from company_corpus.opendata.batches import issuer_rows  # noqa: E402
from company_corpus.opendata.statements import (  # noqa: E402
    _SUB_COLUMNS,
    BULK_URL,
    KEPT_STATEMENTS,
    StatementsReport,
    _extract,
    _rows,
    archive_root,
    load_statements,
)
from company_corpus.sources.edgar_submissions import SUBMISSIONS_URL  # noqa: E402
from company_corpus.sources.edgar_xbrl import COMPANYFACTS_URL  # noqa: E402

CONTACT_ENV = "MOF_SEC_CONTACT"
MANIFEST = "manifest.json"
FSDS_FILE = "fsds.jsonl"
FACTS_FILE = "companyfacts.json"


def write_gz(path: Path, body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as gz:
        gz.write(body)


class Recorder:
    """A ``SecGet`` that keeps every response body (``None`` for a 404), never a header."""

    def __init__(self, sec):
        self.sec, self.bodies = sec, {}

    def get_bytes(self, url: str, *, max_age_s: float | None = None) -> bytes:
        try:
            body = self.sec.get_bytes(url, max_age_s=max_age_s)
        except SecNotFound:
            self.bodies[url] = None
            raise
        self.bodies[url] = body
        return body


def dictionary_tags() -> set[str]:
    """Every ``ns:Tag`` the dictionary reads: chains, statement-only tags, computed totals."""
    d = load_dictionary()
    tags: set[str] = set()
    for table in d.tables:
        for e in table.entries.values():
            tags.update(e.tags, e.statement_only_tags)
            if e.computed:
                tags.add(e.computed.total_tag)
                tags.update(e.computed.minus_first_tag_of)
    return tags


def trim_facts(facts: dict, keep: set[str]) -> dict:
    """``facts`` with only the ``ns:Tag`` entries of ``keep`` (top-level fields unchanged)."""
    out = {k: v for k, v in facts.items() if k != "facts"}
    out["facts"] = {}
    for ns, tags in (facts.get("facts") or {}).items():
        kept = {t: body for t, body in tags.items() if f"{ns}:{t}" in keep}
        if kept:
            out["facts"][ns] = kept
    return out


def _selection_inputs(cik, archive, facts, statements, config, now) -> tuple[list, list]:
    """The rows the selection reads: ``fundamentals`` and ``filing_statements``, canonically
    ordered."""
    rows = issuer_rows(cik, archive, facts, statements, manifest_doc_ids=set(), config=config,
                       now=now)
    key = lambda r: json.dumps(r, sort_keys=True, default=str)  # noqa: E731
    return sorted(rows.fundamentals, key=key), sorted(rows.statements, key=key)


def _fact_accessions(facts: dict) -> set[str]:
    return {p.get("accn") for tags in (facts.get("facts") or {}).values() for body in tags.values()
            for points in (body.get("units") or {}).values() for p in points or []}


def trim_block(block: dict, keep: set[str]) -> dict:
    """A columnar submissions block keeping the rows whose accession is in ``keep``."""
    accns = block.get("accessionNumber") or []
    idx = [i for i, a in enumerate(accns) if a in keep]
    return {k: ([v[i] for i in idx] if isinstance(v, list) and len(v) == len(accns) else v)
            for k, v in block.items()}


def _dump(obj) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


def trim_submissions(bodies: dict[str, bytes], main_url: str, keep: set[str]) -> dict[str, bytes]:
    out = {}
    for url, body in bodies.items():
        doc = json.loads(body)
        if url == main_url:
            doc["filings"]["recent"] = trim_block(doc["filings"]["recent"], keep)
        else:
            doc = trim_block(doc, keep)
        out[url] = _dump(doc)
    return out


def _archive(cik, bodies: dict[str, bytes], now):
    return fetch_submissions(cik, _Static(bodies), max_age_s=None, now=now)


def _quarter_zips(sec, quarters, config) -> list[str]:
    root = archive_root(config)
    root.mkdir(parents=True, exist_ok=True)
    for q in quarters:
        print(f"downloading FSDS {q} ...", file=sys.stderr)
        (root / f"{q}.zip").write_bytes(sec.get_bytes(BULK_URL.format(quarter=q), max_age_s=None))
    return list(quarters)


def _fsds_accessions(config, quarters, wanted: dict[str, str]) -> dict[str, set[str]]:
    """``{cik: accessions}`` that FSDS ``sub.txt`` of the quarters lists for the wanted issuers."""
    out: dict[str, set[str]] = {c: set() for c in wanted.values()}
    for q in quarters:
        with zipfile.ZipFile(archive_root(config) / f"{q}.zip") as zf:
            for r in _rows(zf, "sub.txt", _SUB_COLUMNS):
                if r["cik"] in wanted:
                    out[wanted[r["cik"]]].add(r["adsh"])
    return out


def _relpath(url: str, cik: str) -> str:
    path = urlsplit(url).path
    if url == COMPANYFACTS_URL.format(cik=cik):
        return f"{cik}/{FACTS_FILE}"
    if path.startswith("/submissions/"):
        return f"{cik}/submissions/{path.rsplit('/', 1)[-1]}"
    if path.startswith("/Archives/edgar/data/"):
        parts = path.split("/")   # /Archives/edgar/data/<cik>/<acc>/<file>
        return f"{cik}/{parts[5]}/{parts[6]}"
    raise ValueError(f"unexpected URL {url}")


def record(args, sec) -> int:
    out = Path(args.out)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    since = date.fromisoformat(args.since)
    keep = dictionary_tags()
    manifest_path = out / MANIFEST
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file()
                else {"issuers": {}})
    manifest.update({"recorded_at": now.isoformat(), "since": args.since,
                     "quarters": list(args.quarters), "fsds_url": BULK_URL,
                     "forms": list(args.forms)})
    ciks = [normalize_cik(c) for c in args.ciks]
    with tempfile.TemporaryDirectory(prefix="fsds-") as tmp:
        config = Config(data_dir=Path(tmp), contact="")
        quarters = _quarter_zips(sec, args.quarters, config)
        wanted = {str(int(c)): c for c in ciks}
        in_fsds = _fsds_accessions(config, quarters, wanted)
        _extract(config, ciks, quarters, StatementsReport(write=True))
        for cik in ciks:
            rec = Recorder(sec)
            raw_facts = rec.get_bytes(COMPANYFACTS_URL.format(cik=cik))
            archive = fetch_submissions(cik, rec, max_age_s=None, now=now)
            refs = [r for r in list_filings(archive, forms=args.forms, period_from=since,
                                            period_to=None) if r.accession in in_fsds[cik]]
            counters: Counter = Counter()
            live_rows = 0
            for ref in refs:
                live_rows += len(filing_positions(ref, rec, counters=counters))
            accns = {r.accession for r in refs}
            fsds = [r for r in (load_statements(config, cik) or []) if r["accn"] in accns]
            facts = json.loads(raw_facts)
            live = sorted((r for ref in refs for r in filing_positions(ref, _Static(rec.bodies))),
                          key=lambda r: (r["accn"], r["statement"], r["tag"]))
            subs = {u: b for u, b in rec.bodies.items()
                    if b is not None and urlsplit(u).path.startswith("/submissions/")}
            main_url = SUBMISSIONS_URL.format(cik=cik)
            trimmed, trim_note, sub_note = facts, "full file (--no-trim)", "full files (--no-trim)"
            if not args.no_trim:
                full = _selection_inputs(cik, archive, facts, live, config, now)
                candidate = trim_facts(facts, keep)
                if _selection_inputs(cik, archive, candidate, live, config, now) == full:
                    trimmed, trim_note = candidate, "dictionary tags only; selection inputs equal"
                else:
                    trim_note = "full file: trimming changed the selection inputs"
                small = trim_submissions(subs, main_url, _fact_accessions(trimmed) | accns)
                if _selection_inputs(cik, _archive(cik, small, now), trimmed, live, config,
                                     now) == full:
                    subs, sub_note = small, ("filings of the companyfacts accessions and the "
                                             "recorded ones; selection inputs equal")
                else:
                    sub_note = "full files: trimming changed the selection inputs"
            files: dict[str, str | None] = {}
            for url, body in rec.bodies.items():
                rel = _relpath(url, cik)
                if body is None:
                    files[url] = None
                    continue
                if rel.endswith(f"/{FACTS_FILE}"):
                    body = _dump(trimmed)
                body = subs.get(url, body)
                write_gz(out / f"{rel}.gz", body)
                files[url] = f"{rel}.gz"
            write_gz(out / cik / f"{FSDS_FILE}.gz",
                     "".join(json.dumps(r, sort_keys=True) + "\n" for r in fsds).encode("utf-8"))
            manifest["issuers"][cik] = {
                "name": facts.get("entityName"), "recorded_at": now.isoformat(),
                "companyfacts": trim_note,
                "submissions": sub_note,
                "accessions": [{"accn": r.accession, "form": r.form,
                                "report_date": r.report_date.isoformat() if r.report_date else None,
                                "filed": r.filed.isoformat() if r.filed else None} for r in refs],
                "fsds": f"{cik}/{FSDS_FILE}.gz", "files": dict(sorted(files.items()))}
            print(f"{cik} {facts.get('entityName')}: {len(refs)} filings, live rows {live_rows}, "
                  f"fsds rows {len(fsds)}, {dict(counters)}; companyfacts: {trim_note}; "
                  f"submissions: {sub_note}",
                  file=sys.stderr)
    out.mkdir(parents=True, exist_ok=True)
    manifest["issuers"] = dict(sorted(manifest["issuers"].items()))
    manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def find_amendments(args, sec) -> int:
    """10-K/A of the quarters with primary statement rows in FSDS (rows per statement)."""
    with tempfile.TemporaryDirectory(prefix="fsds-") as tmp:
        config = Config(data_dir=Path(tmp), contact="")
        for q in _quarter_zips(sec, args.quarters, config):
            with zipfile.ZipFile(archive_root(config) / f"{q}.zip") as zf:
                subs = {r["adsh"]: r for r in _rows(zf, "sub.txt", (*_SUB_COLUMNS, "name"))
                        if r["form"] == "10-K/A"}
                counts: dict[str, Counter] = {a: Counter() for a in subs}
                for r in _rows(zf, "pre.txt", ("adsh", "stmt", "inpth")):
                    if r["adsh"] in counts and r["inpth"] == "0" and r["stmt"] in KEPT_STATEMENTS:
                        counts[r["adsh"]][r["stmt"]] += 1
            full = [(a, c) for a, c in counts.items() if all(c[s] for s in KEPT_STATEMENTS)]
            print(f"{q}: {len(subs)} 10-K/A, {len(full)} with BS, IS and CF rows")
            for a, c in sorted(full, key=lambda x: -sum(x[1].values()))[:args.top]:
                s = subs[a]
                print(f"  {s['cik']:>10} {a} {s['name'][:40]:40} period {s['period']} "
                      f"filed {s['filed']} rows {dict(c)}")
    return 0


class _Static:
    def __init__(self, routes):
        self.routes = routes

    def get_bytes(self, url, *, max_age_s=None):
        if self.routes.get(url) is None:
            raise SecNotFound(url)
        return self.routes[url]


def _convert_child(ref: FilingRef, url: str, html: bytes, queue) -> None:
    """Runs in a fresh process: convert and detect sections, report time and peak RSS."""
    import resource

    from company_corpus.live.documents import filing_document
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    t0 = time.perf_counter()
    doc = filing_document(ref, _Static({url: html}))
    seconds = time.perf_counter() - t0
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    item7 = next((s for s in doc.sections if s.name == "Item 7"), None)
    queue.put({"seconds": seconds, "peak_mb": peak / 1024, "delta_mb": (peak - base) / 1024,
               "markdown_chars": len(doc.markdown),
               "sections": [(s.name, s.start, s.end - s.start) for s in doc.sections],
               "item7": None if item7 is None else {
                   "start": item7.start, "length": item7.end - item7.start,
                   "head": " ".join(doc.markdown[item7.start:item7.start + 160].split())}})


def sections(args, sec) -> int:
    now = datetime.now(timezone.utc)
    ctx = multiprocessing.get_context("spawn")
    rows = []
    for cik in (normalize_cik(c) for c in args.ciks):
        archive = fetch_submissions(cik, sec, max_age_s=None, now=now)
        refs = [r for r in list_filings(archive, forms=["10-K"], period_from=None, period_to=None)
                if r.form == "10-K" and r.primary_document]
        if not refs:
            print(f"{cik}: no 10-K", file=sys.stderr)
            continue
        ref = refs[0]
        url = f"{ref.url}/{ref.primary_document}"
        html = sec.get_bytes(url, max_age_s=None)
        queue = ctx.Queue()
        proc = ctx.Process(target=_convert_child, args=(ref, url, html, queue))
        proc.start()
        result = queue.get()
        proc.join()
        rows.append((cik, ref, len(html), result))
    print("| CIK | accession | HTML MB | Markdown chars | s | peak RSS MB (delta) | sections | "
          "Item 7 start | Item 7 length | Item 7 opens with |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for cik, ref, size, r in rows:
        i7 = r["item7"]
        names = " ".join(n.replace("Item ", "") for n, _, _ in r["sections"])
        print(f"| {cik} | {ref.accession} | {size / 1e6:.1f} | {r['markdown_chars']} | "
              f"{r['seconds']:.1f} | {r['peak_mb']:.0f} ({r['delta_mb']:.0f}) | {names} | "
              f"{i7['start'] if i7 else '-'} | {i7['length'] if i7 else '-'} | "
              f"{i7['head'][:100] if i7 else 'not detected'} |")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--ciks", nargs="+", required=True)
    p.add_argument("--since", default="2022-01-01", help="first report date (YYYY-MM-DD)")
    p.add_argument("--quarters", nargs="+", default=[], help="FSDS quarters, e.g. 2025q1 2025q3")
    p.add_argument("--forms", nargs="+", default=list(POSITIONS_FORMS))
    p.add_argument("--out", default=str(ROOT / "tests" / "fixtures" / "live" / "recorded"))
    p.add_argument("--no-trim", action="store_true", help="keep companyfacts whole")
    p.add_argument("--find-amendments", action="store_true")
    p.add_argument("--top", type=int, default=15, help="--find-amendments: candidates shown")
    p.add_argument("--sections", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    contact = os.environ.get(CONTACT_ENV, "").strip()
    if not contact:
        print(f"{CONTACT_ENV} is not set: the SEC asks for a contact (e.g. an email address) "
              "in the User-Agent of automated requests", file=sys.stderr)
        return 2
    sec = SecClient(contact)
    if args.sections:
        return sections(args, sec)
    if not args.quarters:
        print("--quarters is required", file=sys.stderr)
        return 2
    if args.find_amendments:
        return find_amendments(args, sec)
    return record(args, sec)


if __name__ == "__main__":
    sys.exit(main())
