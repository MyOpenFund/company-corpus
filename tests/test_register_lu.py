"""Tests for the Luxembourg LBR/STATEC eCDF register parser (stdlib path)."""
from pathlib import Path

import pytest

from company_corpus.registers.lu_ecdf import parse_lu_declarers
from company_corpus.registers.concepts_lu import map_lu_entity
from company_corpus.financials import compute_derived

FIXTURES = Path(__file__).parent / "fixtures" / "lu"
FERRERO = FIXTURES / "ferrero_b60814_full_2022.xml"
KROKUS = FIXTURES / "krokus_b138357_full_2012.xml"
SILVERSTEIN = FIXTURES / "silverstein_b154370_abr_2022.xml"


def _declarer(result, rcs):
    matches = [d for d in result if d["rcs"] == rcs]
    assert matches, f"No declarer with rcs={rcs!r}"
    return matches[0]


def _declaration(declarer, type_):
    matches = [d for d in declarer["declarations"] if d["type"] == type_]
    assert matches, f"No declaration type={type_!r} in {declarer['rcs']}"
    return matches[0]


class TestFerrero:
    def test_rcs_and_name(self):
        result = parse_lu_declarers(FERRERO)
        d = _declarer(result, "B60814")
        assert d["name"] == "FERRERO INTERNATIONAL S.A."

    def test_bilan_fields(self):
        result = parse_lu_declarers(FERRERO)
        d = _declarer(result, "B60814")
        dec = _declaration(d, "CA_BILAN")
        assert dec["currency"] == "EUR"
        assert dec["fields"][201] == 8721873385.0

    def test_compp_fields(self):
        result = parse_lu_declarers(FERRERO)
        d = _declarer(result, "B60814")
        dec = _declaration(d, "CA_COMPP")
        assert dec["fields"][701] == 237229504.0


class TestKrokus:
    def test_rcs_and_name(self):
        result = parse_lu_declarers(KROKUS)
        d = _declarer(result, "B138357")
        assert d["name"] == "KROKUS S.A."

    def test_bilan_fields(self):
        result = parse_lu_declarers(KROKUS)
        d = _declarer(result, "B138357")
        dec = _declaration(d, "CA_BILAN")
        assert dec["currency"] == "EUR"
        assert dec["fields"][201] == 2182897.55


class TestSilverstein:
    def test_parses_without_error(self):
        result = parse_lu_declarers(SILVERSTEIN)
        assert len(result) >= 1

    def test_rcs(self):
        result = parse_lu_declarers(SILVERSTEIN)
        d = _declarer(result, "B154370")
        assert "Silverstein" in d["name"]

    def test_abridged_bilan_total_assets(self):
        result = parse_lu_declarers(SILVERSTEIN)
        d = _declarer(result, "B154370")
        dec = _declaration(d, "CA_BILANABR")
        assert dec["fields"][201] == 1386627.13


class TestBytesInput:
    def test_bytes_path_equivalence(self):
        """parse_lu_declarers must accept raw bytes as well as a file path."""
        result_path = parse_lu_declarers(KROKUS)
        result_bytes = parse_lu_declarers(KROKUS.read_bytes())
        assert result_path == result_bytes


class TestISO885915Encoding:
    def test_iso_8859_15_bytes_with_accented_name(self):
        """Parser must handle real ISO-8859-15 STATEC dumps without ParseError.

        The raw bytes contain \xe9 (é in ISO-8859-15) and the XML declaration
        says encoding="ISO-8859-15".  expat only supports a small set of
        built-in encodings; passing the raw bytes (or a str re-encoded to
        UTF-8 with the wrong declaration) would raise xml.etree.ParseError on
        many platforms.  The fix in _parse_root re-encodes to UTF-8 and
        rewrites the declaration before handing bytes to ET.fromstring.
        """
        xml = (
            b'<?xml version="1.0" encoding="ISO-8859-15"?>'
            b"<STATECCDBDeclarations>"
            b"<Declarer>"
            b"<RcsNumber>B99999</RcsNumber>"
            b"<LegalUnitName>Soci\xe9t\xe9 S.A.</LegalUnitName>"
            b"</Declarer>"
            b"</STATECCDBDeclarations>"
        )
        result = parse_lu_declarers(xml)
        assert len(result) == 1
        assert result[0]["rcs"] == "B99999"
        assert result[0]["name"] == "Société S.A."


# --------------------------------------------------------------------------- #
# Concept pack (`map_lu_entity`) + NO-FALSE-DATA confidence gate.              #
# Asserted against the REAL validated values (parse fixture -> map).           #
# --------------------------------------------------------------------------- #

def _map(path, rcs):
    """Parse a fixture and map the one declarer's BS+P&L declarations."""
    d = _declarer(parse_lu_declarers(path), rcs)
    return map_lu_entity(d["declarations"])


def _val(res, key):
    """The numeric value emitted for ``key`` (KeyError if suppressed/absent)."""
    return res["values"][key]["value"]


def _suppressed_keys(res):
    return {k for k, _ in res["suppressed"]}


class TestFerreroMap:
    """FERRERO INTERNATIONAL S.A. — 2022 taxonomy, full BS holdco."""

    def _res(self):
        return _map(FERRERO, "B60814")

    def test_not_unbalanced(self):
        assert self._res()["unbalanced"] is False

    def test_balance_sheet_core(self):
        r = self._res()
        assert _val(r, "assets") == pytest.approx(8_721_873_385, abs=0.01)
        assert _val(r, "equity") == pytest.approx(3_545_668_561, abs=0.01)
        assert _val(r, "liabilities") == pytest.approx(5_170_122_402, abs=0.01)
        assert _val(r, "provisions") == pytest.approx(4_479_350, abs=0.01)
        assert _val(r, "cash") == pytest.approx(19_581_809, abs=0.01)

    def test_income_statement(self):
        r = self._res()
        assert _val(r, "revenue") == pytest.approx(237_229_504, abs=0.01)
        assert _val(r, "participation_income") == pytest.approx(1_014_702_785, abs=0.01)

    def test_net_income_uses_669_not_667(self):
        # 669 is the FINAL result (667 + other taxes). Never fall back to 667.
        r = self._res()
        assert _val(r, "net_income") == pytest.approx(677_206_437, abs=0.01)
        assert r["values"]["net_income"]["tag"] == "ecdf:669"

    def test_income_tax_sign_negated_v2022(self):
        # raw ecdf:635 = -107,661,564 (signed expense) -> emit +107,661,564.
        r = self._res()
        assert _val(r, "income_tax") == pytest.approx(107_661_564, abs=0.01)

    def test_interest_expense_abs_v2022(self):
        # raw ecdf:627 = -153,611,574 -> emit abs = +153,611,574.
        r = self._res()
        assert _val(r, "interest_expense") == pytest.approx(153_611_574, abs=0.01)

    def test_debt_split_reconciles_and_feeds_engine_total_debt(self):
        # Engine-consumed maturity split: LT (443+449+359) & ST (441+447+357).
        # It reconciles with the bonds+bank borrowings (437 3.17bn + 355 1.37bn =
        # 4.54bn), so both halves are emitted. NO dead financial_debt key.
        r = self._res()
        lt = _val(r, "long_term_debt")
        st = _val(r, "short_term_debt")
        assert lt == pytest.approx(4_137_307_291, abs=0.01)
        assert st == pytest.approx(403_466_667, abs=0.01)
        assert "financial_debt" not in r["values"]
        # The engine (compute_derived) builds total_debt = LT + ST = borrowings.
        derived = compute_derived(r["values"], currency="EUR")
        assert derived["total_debt"]["value"] == pytest.approx(4_540_773_958, abs=0.01)


class TestKrokusMap:
    """KROKUS S.A. — 2012 taxonomy, full BS."""

    def _res(self):
        return _map(KROKUS, "B138357")

    def test_not_unbalanced(self):
        assert self._res()["unbalanced"] is False

    def test_assets(self):
        assert _val(self._res(), "assets") == pytest.approx(2_182_897.55, abs=0.01)

    def test_debt_split_2012_codes(self):
        # 2012 codes: LT = 347+353+359, ST = 351+357 (=0); reconciles with the
        # 341+355 borrowings total (1,576,436.52). NO dead financial_debt key.
        r = self._res()
        assert _val(r, "long_term_debt") == pytest.approx(1_576_436.52, abs=0.01)
        assert _val(r, "short_term_debt") == pytest.approx(0.0, abs=0.01)
        assert "financial_debt" not in r["values"]

    def test_net_income_639_minus_735(self):
        r = self._res()
        assert _val(r, "net_income") == pytest.approx(24_384.16, abs=0.01)
        assert r["values"]["net_income"]["tag"] == "ecdf:639-735"


class TestSilversteinMap:
    """Silverstein CEE Holdings S.à r.l. — 2022 taxonomy, ABRIDGED BS."""

    def _res(self):
        return _map(SILVERSTEIN, "B154370")

    def test_not_unbalanced(self):
        assert self._res()["unbalanced"] is False

    def test_equity_and_assets_present(self):
        r = self._res()
        assert _val(r, "equity") == pytest.approx(794_133.18, abs=0.01)
        assert _val(r, "assets") == pytest.approx(1_386_627.13, abs=0.01)

    def test_revenue_suppressed_on_abridged(self):
        r = self._res()
        assert "revenue" not in r["values"]
        assert "revenue" in _suppressed_keys(r)

    def test_debt_block_suppressed_on_abridged(self):
        # Abridged gives only aggregate liabilities (435), not borrowings.
        r = self._res()
        for key in ("short_term_debt", "long_term_debt"):
            assert key not in r["values"]
            assert key in _suppressed_keys(r)
        # No dead financial_debt key is emitted.
        assert "financial_debt" not in r["values"]

    def test_net_income_from_669_not_667(self):
        # 667 = -39,414.70 (pre other-taxes) must NEVER be used; 669 = -44,229.70.
        r = self._res()
        assert _val(r, "net_income") == pytest.approx(-44_229.70, abs=0.01)
        assert _val(r, "net_income") != pytest.approx(-39_414.70, abs=0.01)
        assert r["values"]["net_income"]["tag"] == "ecdf:669"


class TestSyntheticGates:
    """Confidence-gate edge cases on constructed declarations."""

    def test_primary_gate_unbalanced_empties_values(self):
        # 201 != 405 beyond tol -> unbalanced, no values emitted.
        decls = [{
            "type": "CA_BILAN", "model": "2", "currency": "EUR",
            "period_end": "2022-12-31",
            "fields": {201: 1_000.0, 405: 5_000.0, 301: 1_000.0},
        }]
        r = map_lu_entity(decls)
        assert r["unbalanced"] is True
        assert r["values"] == {}
        assert any(k == "__all__" for k, _ in r["suppressed"])

    def test_debt_maturity_mismatch_suppresses_whole_block(self):
        # Full v2022 BS that balances (primary + structural), borrowings formable
        # (437+355 = 4000), but the maturity split ST+LT (300) != 4000 -> the
        # split is unconfirmable, so the WHOLE debt block is suppressed. There is
        # NO financial_debt fallback (the engine has no such key).
        decls = [{
            "type": "CA_BILAN", "model": "2", "currency": "EUR",
            "period_end": "2022-12-31",
            "fields": {
                201: 10_000.0, 405: 10_000.0,   # primary balances
                301: 4_000.0, 435: 6_000.0,     # structural: 4000+0+6000+0 == 10000
                669: 0.0,                        # marks the 2022 taxonomy
                437: 3_000.0, 355: 1_000.0,      # borrowings = 4000
                441: 100.0, 443: 200.0,          # ST=100, LT=200 -> 300 != 4000
            },
        }]
        r = map_lu_entity(decls)
        assert "financial_debt" not in r["values"]
        assert "short_term_debt" not in r["values"]
        assert "long_term_debt" not in r["values"]
        assert "short_term_debt" in _suppressed_keys(r)
        assert "long_term_debt" in _suppressed_keys(r)

    def test_version_detection_uses_version_exclusive_codes(self):
        # A 2022 balance sheet filed WITHOUT any P&L (no 669) must still read as
        # 2022 via the version-exclusive BS codes 435/437, so the 2022 debt codes
        # (437/355 bonds+bank, 441/447/357 ST, 443/449/359 LT) are used.
        decls = [{
            "type": "CA_BILAN", "model": "2", "currency": "EUR",
            "period_end": "2022-12-31",
            "fields": {
                201: 10_000.0, 405: 10_000.0,   # primary balances
                301: 4_000.0, 435: 6_000.0,     # structural: 4000+0+6000+0 == 10000
                437: 3_000.0, 355: 1_000.0,      # 2022 bonds+bank = 4000 (no 669)
                441: 1_000.0, 443: 3_000.0,      # ST=1000, LT=3000 -> 4000 == 4000
            },
        }]
        r = map_lu_entity(decls)
        # 435 is the 2022 total-liabilities code -> emitted as liabilities.
        assert _val(r, "liabilities") == pytest.approx(6_000.0, abs=0.01)
        # 2022 debt codes reconcile -> split emitted (a 2012 misread would not).
        assert _val(r, "long_term_debt") == pytest.approx(3_000.0, abs=0.01)
        assert _val(r, "short_term_debt") == pytest.approx(1_000.0, abs=0.01)

    def test_map_lu_entity_uses_only_latest_period(self):
        # Two periods of ONE RCS passed together: the guard keeps only the LATEST
        # period_end. The older period is listed LAST (so a naive last-wins merge
        # would apply it) and is internally unbalanced; the latest period balances.
        latest = {
            "type": "CA_BILAN", "model": "2", "currency": "EUR",
            "period_end": "2022-12-31",
            "fields": {201: 10_000.0, 405: 10_000.0, 301: 10_000.0},
        }
        older = {
            "type": "CA_BILAN", "model": "2", "currency": "EUR",
            "period_end": "2021-12-31",
            "fields": {201: 8_000.0, 405: 3_000.0, 301: 3_000.0},
        }
        r = map_lu_entity([latest, older])
        assert r["unbalanced"] is False
        assert r["period_end"] == "2022-12-31"
        assert _val(r, "assets") == pytest.approx(10_000.0, abs=0.01)
        assert _val(r, "equity") == pytest.approx(10_000.0, abs=0.01)
        # Sanity: the older period ALONE is unbalanced — so a merge would have
        # tripped the primary gate. The guard prevented the blend.
        assert map_lu_entity([older])["unbalanced"] is True


# ---------------------------------------------------------------------------
# LU identity: _norm_rcs + resolve_register_specs LU branch
# ---------------------------------------------------------------------------

def test_norm_rcs_strips_space():
    from company_corpus.registers.identity import _norm_rcs
    assert _norm_rcs("B 60814") == "B60814"


def test_norm_rcs_uppercases():
    from company_corpus.registers.identity import _norm_rcs
    assert _norm_rcs("b6061") == "B6061"


class _GleifFetcherLU:
    """Minimal GLEIF stub returning one LU entity record."""
    def __init__(self, country, registered_as, name="FERRERO INTERNATIONAL S.A."):
        self._c, self._r, self._n = country, registered_as, name

    def get_json(self, url, **kw):
        return {"data": {"attributes": {"entity": {
            "legalName": {"name": self._n},
            "legalAddress": {"country": self._c},
            "registeredAs": self._r,
        }}}}


def test_lu_lei_resolves_via_gleif():
    """LEI for a LU entity resolves via GLEIF entity.registeredAs -> rcs."""
    from company_corpus.registers.identity import resolve_register_specs
    r = resolve_register_specs(
        [{"lei": "L1LU"}],
        fetcher=_GleifFetcherLU("LU", "B6061"),
    )[0]
    assert r["rcs"] == "B6061"
    assert r["country"] == "LU"
    assert r["status"] == "ok"
    assert r["lei"] == "L1LU"


def test_non_lu_lei_unresolved():
    """LEI for a non-LU entity (e.g. FR) stays unresolved; no rcs returned."""
    from company_corpus.registers.identity import resolve_register_specs
    r = resolve_register_specs(
        [{"lei": "L2FR"}],
        fetcher=_GleifFetcherLU("FR", "B6061"),
    )[0]
    assert r["status"] == "unresolved"
    assert not r.get("rcs")


# ---------------------------------------------------------------------------
# lu_cdb: iter_lu_declarers + download_lu_quarter
# ---------------------------------------------------------------------------

_TWO_DECLARER_XML = b"""\
<?xml version="1.0" encoding="UTF-8"?>
<STATECCDBDeclarations>
  <Declarer>
    <RcsNumber>B60814</RcsNumber>
    <LegalUnitName>FERRERO INTERNATIONAL S.A.</LegalUnitName>
    <Declaration type="CA_BILAN" model="2">
      <Currency>EUR</Currency>
      <EndDate>2022-12-31</EndDate>
      <FormData>
        <Field ecdf="201"><Data>1000.0</Data></Field>
      </FormData>
    </Declaration>
  </Declarer>
  <Declarer>
    <RcsNumber>B138357</RcsNumber>
    <LegalUnitName>KROKUS S.A.</LegalUnitName>
    <Declaration type="CA_BILAN" model="2">
      <Currency>EUR</Currency>
      <EndDate>2022-12-31</EndDate>
      <FormData>
        <Field ecdf="201"><Data>2000.0</Data></Field>
      </FormData>
    </Declaration>
  </Declarer>
</STATECCDBDeclarations>
"""


class TestIterLuDeclarers:
    """iter_lu_declarers — no-network tests using a synthetic 2-Declarer XML."""

    def test_yields_both_declarers(self):
        from company_corpus.registers.lu_cdb import iter_lu_declarers
        result = list(iter_lu_declarers(_TWO_DECLARER_XML))
        assert len(result) == 2
        rcs_set = {d["rcs"] for d in result}
        assert rcs_set == {"B60814", "B138357"}

    def test_declarers_have_correct_names(self):
        from company_corpus.registers.lu_cdb import iter_lu_declarers
        result = {d["rcs"]: d for d in iter_lu_declarers(_TWO_DECLARER_XML)}
        assert result["B60814"]["name"] == "FERRERO INTERNATIONAL S.A."
        assert result["B138357"]["name"] == "KROKUS S.A."

    def test_rcs_filter_selects_one(self):
        from company_corpus.registers.lu_cdb import iter_lu_declarers
        result = list(iter_lu_declarers(_TWO_DECLARER_XML, rcs_filter={"B60814"}))
        assert len(result) == 1
        assert result[0]["rcs"] == "B60814"

    def test_rcs_filter_empty_set_yields_nothing(self):
        from company_corpus.registers.lu_cdb import iter_lu_declarers
        result = list(iter_lu_declarers(_TWO_DECLARER_XML, rcs_filter=set()))
        assert result == []

    def test_rcs_filter_none_yields_all(self):
        from company_corpus.registers.lu_cdb import iter_lu_declarers
        result = list(iter_lu_declarers(_TWO_DECLARER_XML, rcs_filter=None))
        assert len(result) == 2

    def test_declarations_preserved(self):
        from company_corpus.registers.lu_cdb import iter_lu_declarers
        result = {d["rcs"]: d for d in iter_lu_declarers(_TWO_DECLARER_XML)}
        dec = result["B60814"]["declarations"][0]
        assert dec["type"] == "CA_BILAN"
        assert dec["currency"] == "EUR"
        assert dec["period_end"] == "2022-12-31"
        assert dec["fields"][201] == 1000.0


class TestDownloadLuQuarter:
    """download_lu_quarter — no-network tests using a stub fetcher."""

    class _StubFetcher:
        def __init__(self, content: bytes):
            self._content = content

        def get(self, url: str, **kw):
            class _Resp:
                pass
            r = _Resp()
            r.content = self._content
            return r

    class _FailingFetcher:
        def get(self, url: str, **kw):
            raise ConnectionError("network unreachable")

    def test_returns_bytes(self):
        from company_corpus.registers.lu_cdb import download_lu_quarter
        payload = b"<fake>xml</fake>"
        fetcher = self._StubFetcher(payload)
        result = download_lu_quarter("https://example.com/q.xml", fetcher=fetcher)
        assert result == payload

    def test_raises_runtime_error_on_failure(self):
        from company_corpus.registers.lu_cdb import download_lu_quarter
        with pytest.raises(RuntimeError, match="Failed to download"):
            download_lu_quarter("https://example.com/q.xml", fetcher=self._FailingFetcher())


# ---------------------------------------------------------------------------
# Task 5: producer (build_lu_financials_from_files) + CLI (--lu-file)
# ---------------------------------------------------------------------------

class TestBuildLuFinancialsFromFiles:
    """build_lu_financials_from_files — keyless local-path producer."""

    def test_writes_jsonl_with_correct_rows(self, tmp_path):
        """write=True writes data/financials_register/B60814.jsonl; rows carry
        source='lbr', country='LU', basis='company', and the validated values."""
        import json
        from company_corpus.config import Config
        from company_corpus.registers.financials import build_lu_financials_from_files

        cfg = Config(data_dir=tmp_path)
        out = build_lu_financials_from_files([str(FERRERO)], config=cfg, write=True)

        # Summary counters
        assert out["entities"] == 1
        assert out["with_financials"] == 1
        assert out["no_financials"] == 0
        assert out["errors"] == 0

        # File written at the expected path
        out_file = tmp_path / "financials_register" / "B60814.jsonl"
        assert out_file.exists(), f"Expected {out_file} to be written"

        rows = [json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
        assert rows, "JSONL must not be empty"

        # Identity columns on every row
        # Ferrero's fiscal year ends 31 August; the fixture carries period_end=2021-08-31.
        # "2022" in the filename is the taxonomy version, not the period.
        for row in rows:
            assert row["source"] == "lbr"
            assert row["country"] == "LU"
            assert row["basis"] == "company"
            assert row["period_end"] == "2021-08-31"
            assert row["fy"] == 2021
            assert row["currency"] == "EUR"

        # Equity = 3,545,668,561 (ecdf:301)
        eq_rows = [r for r in rows if r["concept"] == "equity" and r["kind"] == "reported"]
        assert eq_rows, "equity reported row missing"
        assert round(eq_rows[0]["value"]) == 3_545_668_561

        # Borrowings-based debt_to_equity derived concept present
        dte_rows = [r for r in rows if r["concept"] == "debt_to_equity" and r["kind"] == "derived"]
        assert dte_rows, "debt_to_equity derived row missing"

        # total_debt = long_term_debt + short_term_debt = 4,540,773,958
        td_rows = [r for r in rows if r["concept"] == "total_debt" and r["kind"] == "derived"]
        assert td_rows, "total_debt derived row missing"
        assert round(td_rows[0]["value"]) == 4_540_773_958

        # C1: LU leverage is borrowings-based (real financial-debt lines) -> stamped.
        assert dte_rows[0]["leverage_basis"] == "borrowings"
        assert td_rows[0]["leverage_basis"] == "borrowings"

    def test_dry_run_no_file_written(self, tmp_path):
        """write=False: counters correct, no JSONL written."""
        from company_corpus.config import Config
        from company_corpus.registers.financials import build_lu_financials_from_files

        cfg = Config(data_dir=tmp_path)
        out = build_lu_financials_from_files([str(FERRERO)], config=cfg, write=False)

        assert out["with_financials"] == 1
        out_file = tmp_path / "financials_register" / "B60814.jsonl"
        assert not out_file.exists(), "Dry-run must not write any file"
        assert out["paths"] == []

    def test_rcs_filter_excludes_non_matching(self, tmp_path):
        """rcs_filter restricts which entities are processed; no match -> 0 entities."""
        from company_corpus.config import Config
        from company_corpus.registers.financials import build_lu_financials_from_files

        cfg = Config(data_dir=tmp_path)
        out = build_lu_financials_from_files(
            [str(FERRERO)], config=cfg, write=False, rcs_filter={"BXXX"}
        )
        assert out["entities"] == 0

    def test_rcs_filter_matching_entity_passes(self, tmp_path):
        """rcs_filter={'B60814'} passes Ferrero through."""
        from company_corpus.config import Config
        from company_corpus.registers.financials import build_lu_financials_from_files

        cfg = Config(data_dir=tmp_path)
        out = build_lu_financials_from_files(
            [str(FERRERO)], config=cfg, write=False, rcs_filter={"B60814"}
        )
        assert out["entities"] == 1
        assert out["with_financials"] == 1

    def test_error_isolation(self, tmp_path):
        """A bad path is counted as an error; the good path still processes."""
        from company_corpus.config import Config
        from company_corpus.registers.financials import build_lu_financials_from_files

        cfg = Config(data_dir=tmp_path)
        out = build_lu_financials_from_files(
            ["/nonexistent/lu/B00000.xml", str(FERRERO)],
            config=cfg, write=False,
        )
        assert out["errors"] == 1
        assert out["with_financials"] == 1


class TestCliLuFile:
    """CLI register-financials --lu-file wiring."""

    def test_dry_run_no_write(self, tmp_path):
        """--lu-file dry-run: rc=0, no JSONL written."""
        from company_corpus.cli import main

        rc = main([
            "--data-dir", str(tmp_path),
            "register-financials",
            "--lu-file", str(FERRERO),
        ])
        assert rc == 0
        out_file = tmp_path / "financials_register" / "B60814.jsonl"
        assert not out_file.exists(), "Dry-run must not write any file"

    def test_write_produces_jsonl(self, tmp_path):
        """--lu-file --write: rc=0, JSONL file written."""
        from company_corpus.cli import main

        rc = main([
            "--data-dir", str(tmp_path),
            "register-financials",
            "--lu-file", str(FERRERO),
            "--write",
        ])
        assert rc == 0
        out_file = tmp_path / "financials_register" / "B60814.jsonl"
        assert out_file.exists(), f"Expected {out_file} to exist after --write"

    def test_rcs_filter_via_cli(self, tmp_path):
        """--rcs B60814 passes the Ferrero entity; no --write means dry-run."""
        from company_corpus.cli import main

        rc = main([
            "--data-dir", str(tmp_path),
            "register-financials",
            "--lu-file", str(FERRERO),
            "--rcs", "B60814",
        ])
        assert rc == 0
        # Dry-run: no file written even with matching --rcs
        out_file = tmp_path / "financials_register" / "B60814.jsonl"
        assert not out_file.exists()
