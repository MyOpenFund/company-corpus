from pathlib import Path

import pytest

from company_corpus.registers.concepts_uk import map_ch_facts
from company_corpus.registers.identity import resolve_register_specs

_FIXTURES = Path(__file__).parent / "fixtures" / "uk"


def flat(**kw):
    """Synthetic ``flatten_oim_json`` output: one point per concept at 2025-03-31."""
    return {
        name: [{"val": v, "end": "2025-03-31", "unit": "GBP", "tag": name, "label": name}]
        for name, v in kw.items()
    }


def test_full_clean_filer_all_emitted():   # 00510976
    m = map_ch_facts(flat(FixedAssets=3645291, CurrentAssets=2625095,
        NetCurrentAssetsLiabilities=2484269, TotalAssetsLessCurrentLiabilities=6129560,
        NetAssetsLiabilities=6053560, Equity=6053560, ProfitLoss=161709, CashBankOnHand=2095623))
    v = {k: m["values"][k]["value"] for k in m["values"]}
    assert not m["unbalanced"]
    assert v["equity"] == 6053560 and v["net_income"] == 161709
    assert v["liabilities_current"] == 140826 and v["long_term_debt"] == 76000
    assert v["assets"] == 6270386 and v["liabilities"] == 216826
    # balance identity holds
    assert abs((v["assets"] - v["liabilities"]) - v["equity"]) <= 2


def test_fixedassets_untagged_assets_via_talcl_not_understated():   # 01194034
    m = map_ch_facts(flat(CurrentAssets=1107327, NetCurrentAssetsLiabilities=565497,
        TotalAssetsLessCurrentLiabilities=8893190, NetAssetsLiabilities=7521425,
        Equity=7521425, CashBankOnHand=47547))
    v = {k: m["values"][k]["value"] for k in m["values"]}
    assert v["assets"] == 9435020           # NOT 1107327 (the trap)
    assert v["long_term_debt"] == 1371765 and v["liabilities_current"] == 541830


def test_pl_only_filer_suppresses_unconfirmable_balance():   # SC741022
    m = map_ch_facts(flat(TurnoverRevenue=30927, GrossProfitLoss=30796, OperatingProfitLoss=19924,
        ProfitLossOnOrdinaryActivitiesBeforeTax=19924, TaxTaxCreditOnProfitOrLossOnOrdinaryActivities=4030,
        ProfitLoss=15894, CurrentAssets=22922, NetAssetsLiabilities=18260, Equity=18260, CashBankOnHand=10759))
    v = {k: m["values"][k]["value"] for k in m["values"]}
    assert v["revenue"] == 30927 and v["net_income"] == 15894 and v["equity"] == 18260
    assert "assets" not in v and "liabilities" not in v   # TALCL/NCA absent -> suppressed, not faked


def test_micro_balance_sheet():   # frs105 02855129
    m = map_ch_facts(flat(FixedAssets=0, CurrentAssets=304205,
        NetCurrentAssetsLiabilities=24699, TotalAssetsLessCurrentLiabilities=24699, Equity=24699))
    v = {k: m["values"][k]["value"] for k in m["values"]}
    assert v["assets"] == 304205 and v["liabilities_current"] == 279506 and v["equity"] == 24699
    assert v.get("long_term_debt", 0) == 0


def test_negative_equity_distressed():   # 11515034
    # CurrentAssets absent -> liabilities_current not derivable. long_term_debt
    # (= TALCL - NetAssets = 13002) IS derivable, but emitting it alone would let
    # the engine compute total_debt = 13002, understating true total liabilities.
    # Per the completeness rule the whole derived balance block is suppressed;
    # only directly-tagged equity and the P&L survive.
    m = map_ch_facts(flat(TurnoverRevenue=0, ProfitLoss=0, NetCurrentAssetsLiabilities=-10541,
        TotalAssetsLessCurrentLiabilities=-10541, NetAssetsLiabilities=-23543, Equity=-23543))
    v = {k: m["values"][k]["value"] for k in m["values"]}
    assert v["equity"] == -23543 and v["revenue"] == 0
    assert "net_assets" not in v            # M2: net_assets is no longer emitted
    for key in ("long_term_debt", "liabilities", "liabilities_current",
                "short_term_debt", "assets"):
        assert key not in v
    suppressed_keys = {k for k, _ in m["suppressed"]}
    assert {"long_term_debt", "liabilities_current"} <= suppressed_keys
    assert not m["unbalanced"]


def test_unbalanced_filing_suppressed():   # crafted: NA != E beyond tol
    m = map_ch_facts(flat(NetAssetsLiabilities=1000, Equity=1200, CurrentAssets=5000,
        NetCurrentAssetsLiabilities=1200, TotalAssetsLessCurrentLiabilities=1000))
    assert m["unbalanced"] is True and m["values"] == {}


def test_reconciliation_mismatch_suppresses_all_derived_balance():   # crafted
    # Primary NA==E passes (1100==1100) but the Anchor reconciliation fails:
    # TALCL 1200 != FixedAssets 1000 + NetCurrentAssets 100. The inputs are
    # proven inconsistent, so EVERY derived balance item is suppressed — not just
    # assets/liabilities. Directly-tagged equity still stands; this is a
    # partial suppression, not a whole-filing reject (unbalanced stays False).
    m = map_ch_facts(flat(FixedAssets=1000, CurrentAssets=500, NetCurrentAssetsLiabilities=100,
        TotalAssetsLessCurrentLiabilities=1200, NetAssetsLiabilities=1100, Equity=1100))
    v = {k: m["values"][k]["value"] for k in m["values"]}
    for key in ("assets", "liabilities", "liabilities_current",
                "short_term_debt", "long_term_debt"):
        assert key not in v
    assert v["equity"] == 1100
    assert "net_assets" not in v            # M2: net_assets is no longer emitted
    suppressed_keys = {k for k, _ in m["suppressed"]}
    assert {"assets", "liabilities", "liabilities_current",
            "short_term_debt", "long_term_debt"} <= suppressed_keys
    assert m["unbalanced"] is False


def test_currency_iso4217_guard():
    """Fix #2: share-count and per-share units must not poison the currency field.

    In an iXBRL filing, a share-count fact (unit="shares") or a per-share fact
    (unit="GBP/shares") may be iterated before GBP monetary facts.  The old code
    accepted any truthy unit; the fix accepts only ISO-4217 codes (^[A-Z]{3}$).
    """
    # shares-unit fact comes first in iteration order (Python 3.7+ preserves insertion
    # order in dicts), then a per-share unit, then the GBP monetary facts.
    f = {
        "NumberShares": [{"val": 1_000_000, "end": "2025-03-31",
                          "unit": "shares", "tag": "NumberShares"}],
        "PerShareEarnings": [{"val": 0.05, "end": "2025-03-31",
                              "unit": "GBP/shares", "tag": "PerShareEarnings"}],
        "Equity": [{"val": 50_000, "end": "2025-03-31",
                    "unit": "GBP", "tag": "Equity"}],
        "NetAssetsLiabilities": [{"val": 50_000, "end": "2025-03-31",
                                  "unit": "GBP", "tag": "NetAssetsLiabilities"}],
    }
    m = map_ch_facts(f)
    assert m is not None
    assert m["currency"] == "GBP", f"Expected currency GBP, got {m['currency']!r}"
    # All emitted monetary values must carry GBP, not "shares"
    assert m["values"]["equity"]["unit"] == "GBP"
    assert not m["unbalanced"]


def test_oim_from_ch_html_parses_micro():
    pytest.importorskip("arelle")
    from company_corpus.registers.ch_ixbrl import oim_from_ch_html
    oim = oim_from_ch_html(str(_FIXTURES / "frs105_micro_02855129.html"))
    facts = oim["facts"]
    assert len(facts) > 20
    concepts = {fv["dimensions"]["concept"].split(":")[-1] for fv in facts.values()}
    assert {"CurrentAssets", "Equity"} <= concepts


# ---------------------------------------------------------------------------
# GB identity: _norm_ch_number + resolve_register_specs GB branch
# ---------------------------------------------------------------------------

def test_ch_number_preserved_and_padded():
    from company_corpus.registers.identity import _norm_ch_number
    assert _norm_ch_number("510976") == "00510976"       # pure digits -> zero-pad to 8
    assert _norm_ch_number("SC741022") == "SC741022"     # SC prefix -> verbatim
    assert _norm_ch_number(" oc372294 ") == "OC372294"   # strip + uppercase, OC prefix


class _GleifFetcherGB:
    """Minimal GLEIF stub returning one entity record."""
    def __init__(self, country, registered_as, name="ACME LTD"):
        self._c, self._r, self._n = country, registered_as, name

    def get_json(self, url, **kw):
        return {"data": {"attributes": {"entity": {
            "legalName": {"name": self._n},
            "legalAddress": {"country": self._c},
            "registeredAs": self._r,
        }}}}


def test_gb_lei_resolves_via_gleif():
    """LEI for a GB entity resolves via GLEIF entity.registeredAs -> ch_number."""
    r = resolve_register_specs(
        [{"lei": "L1GB"}],
        fetcher=_GleifFetcherGB("GB", "10399850"),
    )[0]
    assert r["ch_number"] == "10399850"
    assert r["country"] == "GB"
    assert r["status"] == "ok"
    assert r["lei"] == "L1GB"


def test_non_gb_lei_unresolved():
    """LEI for a non-GB entity (e.g. SE) stays unresolved; no ch_number returned."""
    r = resolve_register_specs(
        [{"lei": "L2SE"}],
        fetcher=_GleifFetcherGB("SE", "5560000000"),
    )[0]
    assert r["status"] == "unresolved"
    assert not r.get("ch_number")


# ---------------------------------------------------------------------------
# iter_ch_bulk: keyless bulk zip iterator
# ---------------------------------------------------------------------------

def test_iter_ch_bulk_yields_both_fixtures(tmp_path):
    import zipfile
    from company_corpus.registers.ch_bulk import iter_ch_bulk

    fixture_dir = str(_FIXTURES)
    micro_bytes = open(f"{fixture_dir}/frs105_micro_02855129.html", "rb").read()
    pl_bytes = open(f"{fixture_dir}/frs102_pl_SC741022.html", "rb").read()

    zip_path = tmp_path / "accounts_bulk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Prod223_4212_02855129_20260331.html", micro_bytes)
        zf.writestr("Prod223_4212_SC741022_20250831.html", pl_bytes)

    results = list(iter_ch_bulk(str(zip_path)))
    numbers = {ch for ch, _ in results}
    assert numbers == {"02855129", "SC741022"}
    assert all(len(b) > 0 for _, b in results)


def test_iter_ch_bulk_limit(tmp_path):
    import zipfile
    from company_corpus.registers.ch_bulk import iter_ch_bulk

    fixture_dir = str(_FIXTURES)
    micro_bytes = open(f"{fixture_dir}/frs105_micro_02855129.html", "rb").read()
    pl_bytes = open(f"{fixture_dir}/frs102_pl_SC741022.html", "rb").read()

    zip_path = tmp_path / "accounts_bulk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Prod223_4212_02855129_20260331.html", micro_bytes)
        zf.writestr("Prod223_4212_SC741022_20250831.html", pl_bytes)

    results = list(iter_ch_bulk(str(zip_path), limit=1))
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Synthetic OIM dicts for unit tests (no Arelle required)
# ---------------------------------------------------------------------------
# 02855129 — FRS105 micro balance-sheet only
# Period: year ended 31 March 2026 (instant midnight = 2026-04-01T00:00:00 -> 2026-03-31)
_OIM_MICRO = {
    "documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"},
    "facts": {
        "f0": {"value": "0", "decimals": 0, "dimensions": {
            "concept": "uk-bus:FixedAssets", "period": "2026-04-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f1": {"value": "304205", "decimals": 0, "dimensions": {
            "concept": "uk-bus:CurrentAssets", "period": "2026-04-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f2": {"value": "24699", "decimals": 0, "dimensions": {
            "concept": "uk-bus:NetCurrentAssetsLiabilities", "period": "2026-04-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f3": {"value": "24699", "decimals": 0, "dimensions": {
            "concept": "uk-bus:TotalAssetsLessCurrentLiabilities", "period": "2026-04-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f4": {"value": "24699", "decimals": 0, "dimensions": {
            "concept": "uk-bus:Equity", "period": "2026-04-01T00:00:00",
            "unit": "iso4217:GBP"}},
    },
}

# SC741022 — FRS102 P&L-only filer (TALCL absent -> balance block suppressed)
# Period: year ended 31 August 2025
# Instant items: midnight 2025-09-01T00:00:00 -> 2025-08-31
# Duration items: 2024-09-01T00:00:00/2025-09-01T00:00:00 -> end 2025-08-31
_OIM_PL = {
    "documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"},
    "facts": {
        "f0": {"value": "30927", "decimals": 0, "dimensions": {
            "concept": "uk-bus:TurnoverRevenue",
            "period": "2024-09-01T00:00:00/2025-09-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f1": {"value": "15894", "decimals": 0, "dimensions": {
            "concept": "uk-bus:ProfitLoss",
            "period": "2024-09-01T00:00:00/2025-09-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f2": {"value": "18260", "decimals": 0, "dimensions": {
            "concept": "uk-bus:Equity", "period": "2025-09-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f3": {"value": "18260", "decimals": 0, "dimensions": {
            "concept": "uk-bus:NetAssetsLiabilities", "period": "2025-09-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f4": {"value": "22922", "decimals": 0, "dimensions": {
            "concept": "uk-bus:CurrentAssets", "period": "2025-09-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f5": {"value": "10759", "decimals": 0, "dimensions": {
            "concept": "uk-bus:CashBankOnHand", "period": "2025-09-01T00:00:00",
            "unit": "iso4217:GBP"}},
    },
}


# UNBAL — crafted filing where Equity=24699 but NetAssetsLiabilities=25000
# (difference 301 >> _tol(25000)=125 -> triggers the unbalanced gate in map_ch_facts)
_OIM_UNBALANCED = {
    "documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"},
    "facts": {
        "f0": {"value": "24699", "decimals": 0, "dimensions": {
            "concept": "uk-bus:Equity", "period": "2026-04-01T00:00:00",
            "unit": "iso4217:GBP"}},
        "f1": {"value": "25000", "decimals": 0, "dimensions": {
            "concept": "uk-bus:NetAssetsLiabilities", "period": "2026-04-01T00:00:00",
            "unit": "iso4217:GBP"}},
    },
}


# EQ0001 — equity-only filer: Equity = NetAssetsLiabilities, no goodwill/intangibles.
# The engine would compute tangible_book_value = equity − 0 − 0 = equity, which would
# overstate true TBV for any filer carrying intangibles.  The shared _SUPPRESSED_CONCEPTS
# filter must drop it before the row is written.
_OIM_EQUITY_ONLY = {
    "documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"},
    "facts": {
        "f0": {"value": "100000", "decimals": 0, "dimensions": {
            "concept": "uk-bus:Equity", "period": "2025-12-31T00:00:00",
            "unit": "iso4217:GBP"}},
        "f1": {"value": "100000", "decimals": 0, "dimensions": {
            "concept": "uk-bus:NetAssetsLiabilities", "period": "2025-12-31T00:00:00",
            "unit": "iso4217:GBP"}},
    },
}


def _make_bulk_zip(tmp_path, fixture_dir=str(_FIXTURES)):
    """Build a two-entry bulk zip from the two UK HTML fixtures."""
    import zipfile
    micro_bytes = open(f"{fixture_dir}/frs105_micro_02855129.html", "rb").read()
    pl_bytes = open(f"{fixture_dir}/frs102_pl_SC741022.html", "rb").read()
    zip_path = tmp_path / "accounts_bulk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Prod223_4212_02855129_20260331.html", micro_bytes)
        zf.writestr("Prod223_4212_SC741022_20250831.html", pl_bytes)
    return zip_path, micro_bytes, pl_bytes


# ---------------------------------------------------------------------------
# build_ch_financials — unit (no Arelle; monkeypatched OIM loader)
# ---------------------------------------------------------------------------

class _FakeCntlr:
    """Minimal stub so build_ch_financials skips the Arelle Cntlr() call."""
    def close(self): pass


def test_build_ch_financials_unit(monkeypatch, tmp_path):
    import json
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_ch_financials

    zip_path, micro_bytes, pl_bytes = _make_bulk_zip(tmp_path)

    def fake_oim(html_path, *, cntlr=None):
        content = open(html_path, "rb").read()
        if content == micro_bytes:
            return _OIM_MICRO
        if content == pl_bytes:
            return _OIM_PL
        raise ValueError(f"Unexpected content at {html_path}")

    monkeypatch.setattr("company_corpus.registers.financials.oim_from_ch_html", fake_oim)

    cfg = Config(data_dir=tmp_path)
    rep = build_ch_financials(str(zip_path), config=cfg, write=True, cntlr=_FakeCntlr())

    # Summary counters
    assert rep["entities"] == 2
    assert rep["with_financials"] == 2
    assert rep["no_financials"] == 0
    assert rep["unbalanced"] == 0
    assert rep["errors"] == 0
    assert rep["periods"] == 2

    # 02855129 rows: source / country / basis + values
    path_micro = tmp_path / "financials_register" / "02855129.jsonl"
    rows_micro = [json.loads(x) for x in path_micro.read_text().splitlines()]
    assert rows_micro, "02855129.jsonl must be written"
    assert all(r["source"] == "companies_house" for r in rows_micro)
    assert all(r["country"] == "GB" for r in rows_micro)
    assert all(r["basis"] == "company" for r in rows_micro)
    # assets = TALCL(24699) + liabilities_current(CA-NCA = 304205-24699 = 279506) = 304205
    assets_row = next(r for r in rows_micro if r["kind"] == "reported" and r["concept"] == "assets")
    assert assets_row["value"] == 304205
    equity_row = next(r for r in rows_micro if r["kind"] == "reported" and r["concept"] == "equity")
    assert equity_row["value"] == 24699

    # C1: UK leverage is total-liabilities-based (short_term_debt mirrors current
    # liabilities) -> stamped on the leverage-derived rows.
    dte_row = next(r for r in rows_micro
                   if r["kind"] == "derived" and r["concept"] == "debt_to_equity")
    assert dte_row["leverage_basis"] == "total_liabilities"
    td_row = next(r for r in rows_micro
                  if r["kind"] == "derived" and r["concept"] == "total_debt")
    assert td_row["leverage_basis"] == "total_liabilities"

    # SC741022 rows: revenue + equity
    path_pl = tmp_path / "financials_register" / "SC741022.jsonl"
    rows_pl = [json.loads(x) for x in path_pl.read_text().splitlines()]
    assert rows_pl, "SC741022.jsonl must be written"
    rev_row = next(r for r in rows_pl if r["kind"] == "reported" and r["concept"] == "revenue")
    assert rev_row["value"] == 30927
    eq_row = next(r for r in rows_pl if r["kind"] == "reported" and r["concept"] == "equity")
    assert eq_row["value"] == 18260

    # Coverage written
    assert rep.get("coverage_path") is not None
    cov_path = tmp_path / "reports" / "register_coverage_companies_house.jsonl"
    assert cov_path.exists()
    cov = {c["ch_number"]: c for c in
           (json.loads(x) for x in cov_path.read_text().splitlines())}
    assert cov["02855129"]["status"] == "ok"
    assert cov["SC741022"]["status"] == "ok"


def test_build_ch_financials_dry_run_writes_nothing(monkeypatch, tmp_path):
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_ch_financials

    zip_path, micro_bytes, pl_bytes = _make_bulk_zip(tmp_path)

    def fake_oim(html_path, *, cntlr=None):
        content = open(html_path, "rb").read()
        return _OIM_MICRO if content == micro_bytes else _OIM_PL

    monkeypatch.setattr("company_corpus.registers.financials.oim_from_ch_html", fake_oim)

    cfg = Config(data_dir=tmp_path)
    rep = build_ch_financials(str(zip_path), config=cfg, write=False, cntlr=_FakeCntlr())

    assert rep["coverage_path"] is None
    assert not (tmp_path / "financials_register").exists()
    assert rep["with_financials"] == 2   # counted even in dry-run


def test_build_ch_financials_error_isolation(monkeypatch, tmp_path):
    """One unparseable entity must not abort the whole batch."""
    import json
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_ch_financials

    zip_path, micro_bytes, pl_bytes = _make_bulk_zip(tmp_path)
    call_count = {"n": 0}

    def fake_oim(html_path, *, cntlr=None):
        call_count["n"] += 1
        content = open(html_path, "rb").read()
        if content == micro_bytes:
            raise RuntimeError("synthetic parse failure")
        return _OIM_PL   # SC741022 succeeds

    monkeypatch.setattr("company_corpus.registers.financials.oim_from_ch_html", fake_oim)

    cfg = Config(data_dir=tmp_path)
    rep = build_ch_financials(str(zip_path), config=cfg, write=True, cntlr=_FakeCntlr())

    assert rep["errors"] == 1
    assert rep["with_financials"] == 1
    cov_path = tmp_path / "reports" / "register_coverage_companies_house.jsonl"
    cov = {c["ch_number"]: c for c in
           (json.loads(x) for x in cov_path.read_text().splitlines())}
    assert cov["02855129"]["status"] == "source-error"
    assert cov["SC741022"]["status"] == "ok"


def test_build_ch_financials_unbalanced(monkeypatch, tmp_path):
    """I1: Unbalanced filing (NetAssetsLiabilities != Equity beyond tolerance) must
    be flagged status='unbalanced', counted in out['unbalanced'], not in
    out['no_financials'], and must NOT produce a financials JSONL on disk."""
    import json
    import zipfile
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_ch_financials

    # One-file bulk zip whose OIM will return unbalanced (Equity=24699, NA=25000)
    unbalanced_bytes = b"<html>unbalanced</html>"
    zip_path = tmp_path / "unbalanced_bulk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Prod223_4212_UNBAL01_20260331.html", unbalanced_bytes)

    def fake_oim(html_path, *, cntlr=None):
        return _OIM_UNBALANCED

    monkeypatch.setattr("company_corpus.registers.financials.oim_from_ch_html", fake_oim)

    cfg = Config(data_dir=tmp_path)
    rep = build_ch_financials(str(zip_path), config=cfg, write=True, cntlr=_FakeCntlr())

    # Summary counters: the filing is unbalanced, NOT no-financials
    assert rep["unbalanced"] == 1, f"expected unbalanced=1, got {rep}"
    assert rep["no_financials"] == 0, f"expected no_financials=0, got {rep}"
    assert rep["with_financials"] == 0
    assert rep["entities"] == 1

    # No JSONL written for the unbalanced entity
    assert not (tmp_path / "financials_register" / "UNBAL01.jsonl").exists()

    # Coverage row for this entity must carry status='unbalanced'
    cov_path = tmp_path / "reports" / "register_coverage_companies_house.jsonl"
    assert cov_path.exists(), "coverage file must be written"
    rows = [json.loads(x) for x in cov_path.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["ch_number"] == "UNBAL01"
    assert rows[0]["status"] == "unbalanced"


def test_uk_tangible_book_value_suppressed(monkeypatch, tmp_path):
    """Fix #1: tangible_book_value must never appear in UK output.

    For any filer with no tagged goodwill/intangibles (the FRC-taxonomy norm),
    the engine's TBV collapses to equity — a false number for any company with
    intangibles.  The shared _SUPPRESSED_CONCEPTS filter in _emit_entity_rows
    must drop it before writing, regardless of which producer is used.
    """
    import json
    import zipfile
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_ch_financials

    equity_bytes = b"<html>equity-only</html>"
    zip_path = tmp_path / "equity_bulk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Prod223_4212_EQ0001_20251231.html", equity_bytes)

    def fake_oim(html_path, *, cntlr=None):
        return _OIM_EQUITY_ONLY

    monkeypatch.setattr("company_corpus.registers.financials.oim_from_ch_html", fake_oim)

    cfg = Config(data_dir=tmp_path)
    rep = build_ch_financials(str(zip_path), config=cfg, write=True, cntlr=_FakeCntlr())

    assert rep["with_financials"] == 1
    path = tmp_path / "financials_register" / "EQ0001.jsonl"
    rows = [json.loads(x) for x in path.read_text().splitlines()]
    assert rows, "EQ0001.jsonl must be written"

    concepts = {r.get("concept") for r in rows}
    assert "tangible_book_value" not in concepts, (
        "tangible_book_value must be suppressed for UK — no goodwill/intangibles in FRC"
    )
    assert "tangible_book_value_per_share" not in concepts

    # control: equity itself is still emitted
    assert "equity" in concepts, "equity must still be emitted"


# ---------------------------------------------------------------------------
# build_ch_financials — integration (real Arelle; real HTML fixtures)
# ---------------------------------------------------------------------------

def test_build_ch_financials_integration(tmp_path):
    """End-to-end with real Arelle parse of the two HTML fixtures."""
    pytest.importorskip("arelle")
    import json
    from company_corpus.config import Config
    from company_corpus.registers.financials import build_ch_financials

    zip_path, _, _ = _make_bulk_zip(tmp_path)
    cfg = Config(data_dir=tmp_path)
    rep = build_ch_financials(str(zip_path), config=cfg, write=True)

    assert rep["with_financials"] == 2, f"expected 2 with_financials, got {rep}"

    # SC741022: revenue 30927 + equity 18260
    path_sc = tmp_path / "financials_register" / "SC741022.jsonl"
    rows_sc = [json.loads(x) for x in path_sc.read_text().splitlines()]
    rev = next((r["value"] for r in rows_sc
                if r["kind"] == "reported" and r["concept"] == "revenue"), None)
    assert rev == 30927, f"SC741022 revenue: expected 30927 got {rev}"
    eq_sc = next((r["value"] for r in rows_sc
                  if r["kind"] == "reported" and r["concept"] == "equity"), None)
    assert eq_sc == 18260, f"SC741022 equity: expected 18260 got {eq_sc}"

    # 02855129: assets 304205 + equity 24699
    path_micro = tmp_path / "financials_register" / "02855129.jsonl"
    rows_micro = [json.loads(x) for x in path_micro.read_text().splitlines()]
    assets = next((r["value"] for r in rows_micro
                   if r["kind"] == "reported" and r["concept"] == "assets"), None)
    assert assets == 304205, f"02855129 assets: expected 304205 got {assets}"
    eq_micro = next((r["value"] for r in rows_micro
                     if r["kind"] == "reported" and r["concept"] == "equity"), None)
    assert eq_micro == 24699, f"02855129 equity: expected 24699 got {eq_micro}"


# ---------------------------------------------------------------------------
# CLI: register-financials --ch-bulk
# ---------------------------------------------------------------------------

def test_cli_ch_bulk_dry_run(monkeypatch, tmp_path):
    from company_corpus import cli

    zip_path, _, _ = _make_bulk_zip(tmp_path)
    captured = {}

    def fake_build(zip_path_arg, *, config, write, limit=None, cntlr=None):
        captured.update(zip_path=zip_path_arg, write=write, limit=limit)
        return {"entities": 2, "with_financials": 2, "no_financials": 0,
                "unbalanced": 0, "errors": 0, "periods": 2, "paths": [],
                "coverage_path": None}

    monkeypatch.setattr(cli, "build_ch_financials", fake_build)

    args = cli.build_parser().parse_args(
        ["register-financials", "--ch-bulk", str(zip_path)])
    rc = args.func(args)
    assert rc == 0
    assert captured["write"] is False          # dry-run default
    assert captured["zip_path"] == str(zip_path)
    assert captured["limit"] is None
    # Nothing written
    assert not (tmp_path / "financials_register").exists()


def test_cli_ch_bulk_with_write(monkeypatch, tmp_path):
    from company_corpus import cli

    zip_path, _, _ = _make_bulk_zip(tmp_path)
    captured = {}

    def fake_build(zip_path_arg, *, config, write, limit=None, cntlr=None):
        captured.update(write=write, limit=limit)
        return {"entities": 2, "with_financials": 2, "no_financials": 0,
                "unbalanced": 0, "errors": 0, "periods": 2, "paths": [],
                "coverage_path": str(tmp_path / "reports" / "register_coverage_companies_house.jsonl")}

    monkeypatch.setattr(cli, "build_ch_financials", fake_build)

    args = cli.build_parser().parse_args(
        ["register-financials", "--ch-bulk", str(zip_path), "--write", "--limit", "5"])
    rc = args.func(args)
    assert rc == 0
    assert captured["write"] is True
    assert captured["limit"] == 5


def test_cli_no_register_still_works(monkeypatch, tmp_path):
    """Existing NO path (--orgnrs) must still dispatch to build_register_financials."""
    from company_corpus import cli

    captured = {}

    def fake_no(specs, *, fetcher, config, write):
        captured.update(specs=specs, write=write)
        return {"entities": 1, "with_financials": 1, "periods": 1,
                "coverage_path": None, "paths": []}

    monkeypatch.setattr(cli, "build_register_financials", fake_no)

    args = cli.build_parser().parse_args(
        ["register-financials", "--orgnrs", "923609016"])
    rc = args.func(args)
    assert rc == 0
    assert captured["specs"] == [{"orgnr": "923609016"}]
    assert captured["write"] is False   # dry-run default
