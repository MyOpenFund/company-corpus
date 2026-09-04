"""`eu-acquire`: the EU pillar's acquisition orchestrator behind a schedulable
command. Dry-run by default (discovery only, nothing written); `--write`
downloads + writes manifests, the entity index, the coverage file and the error
trail; `--write --no-download` is a discovery-only run that still leaves its
reports and trail."""
from __future__ import annotations

import json

import pytest

from company_corpus import cli


def _fake_out(**over):
    out = {"entities": 1, "documents": 2, "manifests": 0, "deduped_by_bytes": 0,
           "download_errors": 0, "coverage_path": None, "errors": [], "error_items": [],
           "unresolved": 0, "unresolved_specs": [],
           "sources": {"oam-fr": {"entities": 1, "documents": 2, "errors": 0},
                       "filings.xbrl.org": {"entities": 1, "documents": 0, "errors": 0}}}
    out.update(over)
    out["error_items"] = out["errors"]
    return out


def _install(monkeypatch, tmp_path, out=None, raising=None):
    captured = {}

    def fake_acquire(specs, *, fetcher, config, download=True, write=True):
        captured.update(specs=specs, download=download, write=write, config=config)
        if raising:
            raise raising
        return out if out is not None else _fake_out()

    monkeypatch.setattr(cli, "acquire", fake_acquire)
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: object())
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    return captured


def _report(tmp_path):
    return json.loads((tmp_path / "runs.jsonl").read_text().strip().split("\n")[-1])


def test_eu_acquire_dry_run_discovers_only_and_reports(monkeypatch, tmp_path, capsys):
    captured = _install(monkeypatch, tmp_path)
    rc = cli.main(["eu-acquire", "--isins", "FR0000120271", "--no-download"])
    assert rc == 0
    assert captured["specs"] == [{"isin": "FR0000120271"}]
    assert captured["download"] is False and captured["write"] is False
    rep = _report(tmp_path)
    assert rep["command"] == "eu-acquire" and rep["outcome"] == "ok"
    by_code = {s["source_code"]: s for s in rep["sources"]}
    assert by_code["amf"]["docs_seen"] == 1 and by_code["amf"]["docs_new"] == 2
    assert by_code["xbrlorg"]["docs_seen"] == 1 and by_code["xbrlorg"]["docs_new"] == 0
    assert "DRY-RUN" in capsys.readouterr().out


def test_eu_acquire_default_is_dry_run(monkeypatch, tmp_path):
    captured = _install(monkeypatch, tmp_path)
    assert cli.main(["eu-acquire", "--leis", "L1,L2"]) == 0
    assert captured["specs"] == [{"lei": "L1"}, {"lei": "L2"}]
    assert captured["download"] is False and captured["write"] is False


def test_eu_acquire_write_downloads(monkeypatch, tmp_path):
    captured = _install(monkeypatch, tmp_path)
    assert cli.main(["eu-acquire", "--leis", "L1", "--write"]) == 0
    assert captured["download"] is True and captured["write"] is True


def test_eu_acquire_write_no_download_is_discovery_with_reports(monkeypatch, tmp_path):
    captured = _install(monkeypatch, tmp_path)
    assert cli.main(["eu-acquire", "--leis", "L1", "--write", "--no-download"]) == 0
    assert captured["download"] is False and captured["write"] is True


def test_eu_acquire_dead_backend_is_degraded_with_samples(monkeypatch, tmp_path):
    err = {"source": "oam-fr", "context": "discover", "entity": "L1", "error": "OAM down"}
    out = _fake_out(documents=0, errors=[err],
                    sources={"oam-fr": {"entities": 1, "documents": 0, "errors": 1},
                             "filings.xbrl.org": {"entities": 1, "documents": 0, "errors": 0}})
    _install(monkeypatch, tmp_path, out=out)
    rc = cli.main(["--data-dir", str(tmp_path), "eu-acquire", "--leis", "L1", "--write"])
    rep = _report(tmp_path)
    assert rc == 3 and rep["outcome"] == "degraded"
    amf = [s for s in rep["sources"] if s["source_code"] == "amf"][0]
    assert amf["fetch_errors"] == 1 and "OAM down" in amf["error_samples"][0]
    rows = [json.loads(x) for x in
            (tmp_path / "discovery_errors.jsonl").read_text().splitlines() if x]
    assert rows and rows[-1]["run_id"] == rep["run_id"]


def test_eu_acquire_dead_national_backend_next_to_productive_aggregator_is_ok(
        monkeypatch, tmp_path):
    """A dead national OAM is counted as that backend's own failure on its own
    row; the run degrades only under the zero-useful-work rule, so one document
    from the aggregator keeps the run `ok` while the report names the dead
    authority (`amf` docs_failed 1, `xbrlorg` docs_new 1)."""
    err = {"source": "oam-fr", "context": "discover", "entity": "L1", "error": "OAM down"}
    out = _fake_out(documents=1, errors=[err],
                    sources={"oam-fr": {"entities": 1, "documents": 0, "errors": 1},
                             "filings.xbrl.org": {"entities": 1, "documents": 1, "errors": 0}})
    _install(monkeypatch, tmp_path, out=out)
    rc = cli.main(["eu-acquire", "--leis", "L1"])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    by_code = {s["source_code"]: s for s in rep["sources"]}
    assert by_code["amf"]["docs_failed"] == 1 and by_code["amf"]["docs_new"] == 0
    assert "OAM down" in by_code["amf"]["error_samples"][0]
    assert by_code["xbrlorg"]["docs_new"] == 1 and by_code["xbrlorg"]["docs_failed"] == 0


def test_eu_acquire_prints_partially_unresolved_specs(monkeypatch, tmp_path, capsys):
    """A spec that resolved to no LEI reaches no backend and no report row, so
    the only place it can show is stdout: it is listed by name, in input order,
    and the resolved specs' rows still report normally."""
    out = _fake_out(entities=3, unresolved=2,
                    unresolved_specs=[{"isin": "XX0000000000"}, {"lei": "L9"}])
    _install(monkeypatch, tmp_path, out=out)
    rc = cli.main(["eu-acquire", "--isins", "XX0000000000,FR0000120271"])
    assert rc == 0 and _report(tmp_path)["outcome"] == "ok"
    text = capsys.readouterr().out
    assert "unresolved: ISIN XX0000000000, LEI L9" in text
    assert "(2 unresolved)" in text


def test_eu_acquire_resolved_run_prints_no_unresolved_line(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, tmp_path)
    assert cli.main(["eu-acquire", "--leis", "L1"]) == 0
    assert "unresolved:" not in capsys.readouterr().out


def test_eu_acquire_dry_run_notes_no_download_is_implied(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, tmp_path)
    assert cli.main(["eu-acquire", "--leis", "L1"]) == 0
    assert "note: --no-download is implied without --write" in capsys.readouterr().out
    assert cli.main(["eu-acquire", "--leis", "L1", "--write"]) == 0
    assert "--no-download is implied" not in capsys.readouterr().out


def test_eu_acquire_dry_run_writes_no_trail(monkeypatch, tmp_path):
    err = {"source": "oam-fr", "context": "discover", "entity": "L1", "error": "OAM down"}
    out = _fake_out(documents=0, errors=[err],
                    sources={"oam-fr": {"entities": 1, "documents": 0, "errors": 1}})
    _install(monkeypatch, tmp_path, out=out)
    rc = cli.main(["--data-dir", str(tmp_path), "eu-acquire", "--leis", "L1"])
    assert rc == 3 and _report(tmp_path)["outcome"] == "degraded"
    assert not (tmp_path / "discovery_errors.jsonl").exists()


def test_eu_acquire_crash_is_failed(monkeypatch, tmp_path, capsys):
    _install(monkeypatch, tmp_path, raising=RuntimeError("GLEIF exploded"))
    rc = cli.main(["eu-acquire", "--leis", "L1"])
    assert rc == 1
    rep = _report(tmp_path)
    assert rep["outcome"] == "failed" and "GLEIF exploded" in rep["fatal"]
    assert "error:" in capsys.readouterr().err


def test_eu_acquire_nothing_resolved_is_failed(monkeypatch, tmp_path, capsys):
    """Every spec unresolved: no backend ran, so no source would report and the
    run would pass for a green nothing-to-do. It fails instead."""
    _install(monkeypatch, tmp_path,
             out=_fake_out(documents=0, unresolved=1, sources={},
                           unresolved_specs=[{"isin": "XX0000000000"}]))
    rc = cli.main(["eu-acquire", "--isins", "XX0000000000"])
    assert rc == 1 and _report(tmp_path)["outcome"] == "failed"
    captured = capsys.readouterr()
    # The specs may be bad OR GLEIF may be down: the message must not blame the input alone.
    assert "unresolved" in captured.err and "GLEIF unreachable" in captured.err
    assert "unresolved: ISIN XX0000000000" in captured.out


def test_eu_acquire_requires_leis_or_isins(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["eu-acquire"])
    assert "--leis" in capsys.readouterr().err
    args = cli.build_parser().parse_args(["eu-acquire", "--leis", "L1"])
    assert args.func is cli._cmd_eu_acquire and args.write is False
    assert args.no_download is False


def test_eu_acquire_untagged_error_item_raises(monkeypatch, tmp_path):
    """An error item without a backend `source` cannot be attributed to an
    authority: a producer bug, reported as a failed run — never dropped."""
    out = _fake_out(documents=0, errors=[{"context": "discover", "error": "?"}],
                    sources={"oam-fr": {"entities": 1, "documents": 0, "errors": 1}})
    _install(monkeypatch, tmp_path, out=out)
    rc = cli.main(["eu-acquire", "--leis", "L1"])
    assert rc == 1 and "no backend source" in _report(tmp_path)["fatal"]


def test_eu_acquire_not_indexed_issuer_with_healthy_empty_oam_is_ok(monkeypatch, tmp_path):
    """End to end through the REAL orchestrator and the REAL aggregator backend:
    the national OAM is reachable and lists nothing, filings.xbrl.org answers
    HTTP 404 (issuer not indexed). Nothing is wrong with any source, so the run
    is a clean nothing-to-do (rc 0), not a false-positive exit 3."""
    import requests

    from company_corpus.eu import acquire as acq
    from company_corpus.eu.entities import Entity

    class _Resp:
        status_code = 404

    class _NotIndexedFetcher:
        def get_json(self, url, **_):
            raise requests.HTTPError("404 for " + url, response=_Resp())

    class _HealthyEmptyOam:
        name = "oam-de"
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return []

    monkeypatch.setattr(acq, "resolve_entities",
                        lambda specs, *, fetcher: [Entity("L1", "X", "DE", resolution="lei")])
    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _HealthyEmptyOam})
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: _NotIndexedFetcher())
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "eu-acquire", "--leis", "L1"])
    rep = _report(tmp_path)
    assert rc == 0 and rep["outcome"] == "ok"
    by_code = {s["source_code"]: s for s in rep["sources"]}
    assert by_code["xbrlorg"]["docs_failed"] == 0 and by_code["xbrlorg"]["fetch_errors"] == 0


def test_eu_acquire_truncated_backend_degrades_even_with_documents(monkeypatch, tmp_path):
    """A backend that reported a truncated listing degrades the run (exit 3)
    even though it contributed documents: a partial listing must never look
    complete. The report row carries `truncated: true`."""
    out = _fake_out(documents=2,
                    sources={"oam-fr": {"entities": 1, "documents": 2, "errors": 0,
                                        "truncated": True},
                             "filings.xbrl.org": {"entities": 1, "documents": 0, "errors": 0,
                                                  "truncated": False}})
    _install(monkeypatch, tmp_path, out=out)
    rc = cli.main(["eu-acquire", "--leis", "L1"])
    rep = _report(tmp_path)
    assert rc == 3 and rep["outcome"] == "degraded"
    by_code = {s["source_code"]: s for s in rep["sources"]}
    assert by_code["amf"]["truncated"] is True and by_code["amf"]["docs_new"] == 2
    assert by_code["amf"]["error_samples"], "a truncation with no payload still says why"
    assert by_code["xbrlorg"]["truncated"] is False


def test_eu_acquire_write_all_downloads_forbidden_is_degraded(monkeypatch, tmp_path):
    """End to end through the REAL orchestrator: the listing is fine, every
    file download is refused (403). Nothing was acquired, so the run is
    degraded (exit 3) with docs_new 0 and docs_failed >= 1 — never `ok`."""
    from company_corpus.eu import acquire as acq
    from company_corpus.eu.documents import Document
    from company_corpus.eu.entities import Entity
    from datetime import date

    class _Forbidden:
        def download(self, url, dest):
            raise RuntimeError(f"403 Forbidden for {url}")

    class _OneDoc:
        name = "oam-de"
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e):
            return [Document("de-1", "L1", "DE", "annual_report", date(2023, 12, 31),
                             "2024-04-01", "x", "de", "oam-de",
                             [{"name": "r.pdf", "url": "https://x/r.pdf", "kind": "pdf"}], {})]

    class _NoDocs:
        name = "filings.xbrl.org"
        def __init__(self, *a, **k): self.errors = []
        def discover(self, e): return []

    monkeypatch.setattr(acq, "resolve_entities",
                        lambda specs, *, fetcher: [Entity("L1", "X", "DE", resolution="lei")])
    monkeypatch.setattr(acq, "COUNTRY_BACKENDS", {"DE": _OneDoc})
    monkeypatch.setattr(acq, "FilingsXbrlOrg", _NoDocs)
    monkeypatch.setattr(cli, "Fetcher", lambda cfg: _Forbidden())
    monkeypatch.setenv("COMPANY_DATA_DIR", str(tmp_path))
    rc = cli.main(["--data-dir", str(tmp_path), "eu-acquire", "--leis", "L1", "--write"])
    rep = _report(tmp_path)
    assert rc == 3 and rep["outcome"] == "degraded"
    assert rep["totals"]["docs_new"] == 0 and rep["totals"]["docs_failed"] >= 1
    banz = {s["source_code"]: s for s in rep["sources"]}["banz"]
    assert banz["docs_new"] == 0 and "403" in banz["error_samples"][0]
