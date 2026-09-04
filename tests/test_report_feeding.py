"""Every work command must feed the run report from its own counters.

One test per reporting command: the producer is faked with the REAL report /
``out`` shape it returns today, ``cli.main`` is driven end to end, and the last
line of ``runs.jsonl`` is asserted — totals, the per-source ``source_code``
(resolved through :func:`company_corpus.source_codes.source_code_for`) and the
doctrine outcome. A command that reports zero useful work while errors occurred
must never exit 0.
"""

from __future__ import annotations

import json

import pytest

from company_corpus import cli
from company_corpus.openfigi import FigiRecord
from company_corpus.pipeline import (
    DownloadReport,
    FinancialsReport,
    OwnershipReport,
    RenderReport,
    RunReport,
)
from company_corpus.source_codes import source_code_for
from company_corpus.storage import SaveStats


def _run(monkeypatch, tmp_path, argv: list[str]) -> int:
    """Run the CLI with both the runs path and the data dir pinned to tmp_path."""
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    return cli.main(["--data-dir", str(tmp_path), *argv])


def _report(tmp_path) -> dict:
    lines = (tmp_path / "runs.jsonl").read_text().strip().split("\n")
    return json.loads(lines[-1])


def _source(rep: dict, code: str) -> dict:
    matches = [s for s in rep["sources"] if s["source_code"] == code]
    assert matches, f"no {code!r} source in {rep['sources']}"
    return matches[0]


# --------------------------------------------------------------------------
# discover / discover-index (SEC)
# --------------------------------------------------------------------------
def test_discover_feeds_sec_counters(monkeypatch, tmp_path):
    def fake_discover(ciks, **kw):
        return RunReport(
            rounds=1, issuers=2,
            stats=SaveStats(seen=10, added=4, updated=1, unchanged=5),
            errors=[{"source": "edgar", "context": "0000320193", "error": "429"}],
        )

    monkeypatch.setattr(cli, "discover_universe", fake_discover)
    rc = _run(monkeypatch, tmp_path, ["discover", "--ciks", "0000320193"])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"] == {"docs_seen": 10, "docs_new": 4, "docs_failed": 1}
    src = _source(rep, source_code_for("sec"))
    assert src["fetch_errors"] == 1 and src["error_samples"]


def test_discover_with_download_folds_both_legs(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "discover_universe",
        lambda ciks, **kw: RunReport(rounds=1, issuers=1,
                                     stats=SaveStats(seen=3, added=3)))
    monkeypatch.setattr(
        cli, "download_universe",
        lambda ciks, **kw: DownloadReport(downloaded=2, skipped=1, empty=0, errors=0))
    rc = _run(monkeypatch, tmp_path, ["discover", "--ciks", "0000320193", "--download"])
    rep = _report(tmp_path)
    assert rc == 0
    assert rep["totals"] == {"docs_seen": 6, "docs_new": 5, "docs_failed": 0}


def test_discover_zero_new_with_errors_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "discover_universe",
        lambda ciks, **kw: RunReport(rounds=1, issuers=1, stats=SaveStats(seen=0),
                                     errors=[{"error": "listing unreachable"}]))
    rc = _run(monkeypatch, tmp_path, ["discover", "--ciks", "0000320193"])
    assert rc == 3
    assert _report(tmp_path)["outcome"] == "degraded"


def test_discover_index_feeds_sec_counters(monkeypatch, tmp_path):
    class FakeIndex:
        def __init__(self, **kw):
            self.errors = [{"source": "edgar_index", "error": "index 404"}]

        def discover(self, year, quarter, **kw):
            return []

    class FakeStorage:
        def __init__(self, config=None):
            pass

        def save_records(self, recs, dry_run=False):
            return SaveStats(seen=2, added=1, unchanged=1)

        def record_errors(self, errors):
            pass

    monkeypatch.setattr(cli, "EdgarFullIndex", FakeIndex)
    monkeypatch.setattr(cli, "Storage", FakeStorage)
    rc = _run(monkeypatch, tmp_path,
              ["discover-index", "--ciks", "0000320193", "--years", "2024"])
    rep = _report(tmp_path)
    # four quarters x SaveStats(seen=2, added=1)
    assert rep["totals"] == {"docs_seen": 8, "docs_new": 4, "docs_failed": 1}
    assert _source(rep, "sec")["fetch_errors"] == 1
    assert rc == 0 and rep["outcome"] == "ok"


# --------------------------------------------------------------------------
# download (SEC)
# --------------------------------------------------------------------------
def test_download_zero_downloaded_with_error_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "download_universe",
        lambda ciks, **kw: DownloadReport(
            downloaded=0, skipped=3, empty=0, errors=1, bytes=0,
            error_items=[{"source": "download", "context": "d1", "error": "timeout"}]))
    rc = _run(monkeypatch, tmp_path, ["download", "--ciks", "0000320193"])
    rep = _report(tmp_path)
    assert rc == 3 and rep["outcome"] == "degraded"
    assert rep["totals"] == {"docs_seen": 4, "docs_new": 0, "docs_failed": 1}
    src = _source(rep, "sec")
    assert src["fetch_errors"] == 1
    assert "timeout" in src["error_samples"][0]


def test_download_with_downloads_and_errors_is_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "download_universe",
        lambda ciks, **kw: DownloadReport(
            downloaded=2, skipped=1, empty=1, errors=1, bytes=42,
            error_items=[{"error": "timeout"}]))
    rc = _run(monkeypatch, tmp_path, ["download", "--ciks", "0000320193", "--write"])
    rep = _report(tmp_path)
    # recovered transient errors alongside real new documents do NOT degrade
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"] == {"docs_seen": 5, "docs_new": 2, "docs_failed": 1}


# --------------------------------------------------------------------------
# render-pdf (SEC)
# --------------------------------------------------------------------------
def test_render_pdf_dry_run_counts_would_render_as_new(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "render_universe",
        lambda ciks, **kw: RenderReport(rendered=0, would_render=2, skipped=1,
                                        no_primary=0, errors=0))
    rc = _run(monkeypatch, tmp_path, ["render-pdf", "--ciks", "0000320193"])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"] == {"docs_seen": 3, "docs_new": 2, "docs_failed": 0}


def test_render_pdf_write_mode_counts_rendered(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "render_universe",
        lambda ciks, **kw: RenderReport(rendered=2, would_render=0, skipped=0,
                                        no_primary=1, errors=1,
                                        error_items=[{"error": "chrome crashed"}]))
    rc = _run(monkeypatch, tmp_path, ["render-pdf", "--ciks", "0000320193", "--write"])
    rep = _report(tmp_path)
    assert rc == 0
    assert rep["totals"] == {"docs_seen": 4, "docs_new": 2, "docs_failed": 1}
    assert _source(rep, "sec")["error_samples"]


def test_render_pdf_nothing_rendered_with_errors_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "render_universe",
        lambda ciks, **kw: RenderReport(rendered=0, would_render=0, skipped=0,
                                        no_primary=0, errors=2,
                                        error_items=[{"error": "chrome crashed"}]))
    rc = _run(monkeypatch, tmp_path, ["render-pdf", "--ciks", "0000320193", "--write"])
    assert rc == 3
    assert _report(tmp_path)["outcome"] == "degraded"


# --------------------------------------------------------------------------
# xbrl (SEC)
# --------------------------------------------------------------------------
def test_xbrl_periods_are_the_useful_work(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "fetch_financials",
        lambda ciks, **kw: FinancialsReport(
            issuers=2, periods=5, stats=SaveStats(seen=5, added=5), errors=[]))
    rc = _run(monkeypatch, tmp_path, ["xbrl", "--ciks", "0000320193"])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"] == {"docs_seen": 2, "docs_new": 5, "docs_failed": 0}
    assert _source(rep, "sec")


def test_xbrl_no_periods_with_errors_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "fetch_financials",
        lambda ciks, **kw: FinancialsReport(
            issuers=1, periods=0, errors=[{"error": "companyfacts 500"}]))
    rc = _run(monkeypatch, tmp_path, ["xbrl", "--ciks", "0000320193"])
    rep = _report(tmp_path)
    assert rc == 3 and rep["outcome"] == "degraded"
    assert _source(rep, "sec")["fetch_errors"] == 1


# --------------------------------------------------------------------------
# ownership (SEC)
# --------------------------------------------------------------------------
def test_ownership_sums_parsed_rows_as_new(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "process_ownership",
        lambda ciks, **kw: OwnershipReport(
            issuers=3, downloaded=4, parsed_insider=2, parsed_13f=1, passthrough=1,
            errors=1, error_items=[{"error": "form 4 unparsable"}]))
    rc = _run(monkeypatch, tmp_path, ["ownership", "--ciks", "0000320193"])
    rep = _report(tmp_path)
    assert rc == 0
    assert rep["totals"] == {"docs_seen": 3, "docs_new": 4, "docs_failed": 1}
    assert _source(rep, "sec")["error_samples"]


def test_ownership_nothing_parsed_with_errors_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "process_ownership",
        lambda ciks, **kw: OwnershipReport(issuers=1, downloaded=0, errors=2,
                                           error_items=[{"error": "403"}]))
    rc = _run(monkeypatch, tmp_path, ["ownership", "--ciks", "0000320193"])
    assert rc == 3
    assert _report(tmp_path)["outcome"] == "degraded"


# --------------------------------------------------------------------------
# enrich-openfigi (recorded under the SEC universe it enriches)
# --------------------------------------------------------------------------
def test_enrich_openfigi_counts_mapped_rows(monkeypatch, tmp_path):
    csv_path = tmp_path / "ids.csv"
    csv_path.write_text("isin\nUS0378331005\nUS5949181045\n", encoding="utf-8")
    monkeypatch.setattr(
        cli, "map_identifiers",
        lambda ids, **kw: {ids[0]: FigiRecord(name="Apple Inc.", security_type="Common Stock"),
                           ids[1]: None})
    rc = _run(monkeypatch, tmp_path, ["enrich-openfigi", "--from-file", str(csv_path)])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"] == {"docs_seen": 2, "docs_new": 1, "docs_failed": 0}
    assert _source(rep, "sec")


# --------------------------------------------------------------------------
# eu-financials (filings.xbrl.org / ESEF aggregator)
# --------------------------------------------------------------------------
def test_eu_financials_feeds_xbrlorg(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "build_eu_financials",
        lambda specs, **kw: {"entities": 2, "with_financials": 1, "no_financials": 1,
                             "periods": 3, "paths": [], "coverage_path": None})
    rc = _run(monkeypatch, tmp_path, ["eu-financials", "--leis", "LEI1,LEI2"])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"] == {"docs_seen": 2, "docs_new": 3, "docs_failed": 0}
    assert _source(rep, source_code_for("esef"))["source_code"] == "xbrlorg"


def test_eu_financials_truncated_backend_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "build_eu_financials",
        lambda specs, **kw: {"entities": 1, "with_financials": 1, "no_financials": 0,
                             "periods": 4, "paths": [], "coverage_path": None,
                             "truncated": True, "errors": ["index listing cut short"]})
    rc = _run(monkeypatch, tmp_path, ["eu-financials", "--leis", "LEI1"])
    rep = _report(tmp_path)
    # truncation degrades even though 4 periods were written
    assert rc == 3 and rep["outcome"] == "degraded"
    src = _source(rep, "xbrlorg")
    assert src["truncated"] is True
    assert src["docs_new"] == 4
    assert "index listing cut short" in src["error_samples"][0]


# --------------------------------------------------------------------------
# register-financials (one code per register authority)
# --------------------------------------------------------------------------
def test_register_financials_ee_errors_without_periods_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "build_ee_financials_from_files",
        lambda *a, **kw: {"entities": 4, "with_financials": 0, "no_financials": 2,
                          "unbalanced": 0, "periods": 0, "errors": 2, "paths": []})
    rc = _run(monkeypatch, tmp_path,
              ["register-financials", "--ee-file", "elem.csv", "meta.csv"])
    rep = _report(tmp_path)
    assert rc == 3 and rep["outcome"] == "degraded"
    assert rep["totals"] == {"docs_seen": 4, "docs_new": 0, "docs_failed": 2}
    assert _source(rep, source_code_for("rik"))["source_code"] == "rik"


def test_register_financials_norway_path_feeds_brreg(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "build_register_financials",
        lambda specs, **kw: {"entities": 3, "with_financials": 3, "no_financials": 0,
                             "periods": 6, "errors": 0, "paths": [],
                             "coverage_path": None})
    rc = _run(monkeypatch, tmp_path, ["register-financials", "--orgnrs", "123456789"])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    assert rep["totals"] == {"docs_seen": 3, "docs_new": 6, "docs_failed": 0}
    assert _source(rep, source_code_for("brreg"))


def test_register_financials_uk_bulk_feeds_ukch(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "build_ch_financials",
        lambda *a, **kw: {"entities": 5, "with_financials": 5, "no_financials": 0,
                          "unbalanced": 1, "periods": 5, "errors": 0, "paths": []})
    rc = _run(monkeypatch, tmp_path,
              ["register-financials", "--ch-bulk", str(tmp_path / "accounts.zip")])
    rep = _report(tmp_path)
    assert rc == 0
    assert _source(rep, source_code_for("companies_house"))["source_code"] == "ukch"
    assert rep["totals"]["docs_new"] == 5


def test_register_financials_truncated_backend_is_degraded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cli, "build_sk_financials",
        lambda *a, **kw: {"entities": 2, "with_financials": 2, "no_financials": 0,
                          "unbalanced": 0, "periods": 2, "errors": 0, "paths": [],
                          "truncated": True, "errors_items": []})
    rc = _run(monkeypatch, tmp_path, ["register-financials", "--sk-id", "42"])
    rep = _report(tmp_path)
    assert rc == 3 and rep["outcome"] == "degraded"
    assert _source(rep, "registeruz")["truncated"] is True


# --------------------------------------------------------------------------
# the guard: direct calls (old tests) pass no report
# --------------------------------------------------------------------------
@pytest.mark.parametrize("argv", [
    ["download", "--ciks", "0000320193"],
    ["render-pdf", "--ciks", "0000320193"],
    ["xbrl", "--ciks", "0000320193"],
])
def test_direct_call_without_a_report_still_works(monkeypatch, tmp_path, argv):
    monkeypatch.setattr(cli, "download_universe", lambda ciks, **kw: DownloadReport())
    monkeypatch.setattr(cli, "render_universe", lambda ciks, **kw: RenderReport())
    monkeypatch.setattr(cli, "fetch_financials", lambda ciks, **kw: FinancialsReport())
    args = cli.build_parser().parse_args(["--data-dir", str(tmp_path), *argv])
    assert not hasattr(args, "report")
    assert args.func(args) == 0
