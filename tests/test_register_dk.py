"""Tests for the DK register.

Task 1 — stdlib DK-GAAP FSA XBRL parser (``dk_fsa_xbrl.parse_fsa_facts``).
Task 2 — DK-GAAP concept pack + NO-FALSE-DATA gate (``concepts_dk.map_fsa_facts``).

Fixtures — all real, provenance-clean DK-GAAP class-B filings served verbatim by
Virk/ERST (``distribution.virk.dk`` -> ``regnskaber.virk.dk``):

  * ``dk_30830725_microB_2025`` — NIPE FINANS ApS, micro-B, FY ending 2025-09-30.
  * ``dk_30560000_2024``        — K.Kirch ApS, FY ending 2025-12-31.
  * ``dk_42566551_2025``        — Sneaks2Peak ApS, a FY2025 wind-down. Its
    ``fsa:Provisions`` (15,844) and ``fsa:Revenue`` (36,536) are tagged ONLY in
    the 2024 comparative; the 2025 current period settled them. This is the
    regression fixture for the period-mixing bug: the fixed parser must NOT leak
    those prior-only facts into the current-period view.
  * ``dk_42710644_provisions``  — Taxikørsel 011 ApS, class B, FY2025. Its
    CURRENT period carries ``fsa:Provisions`` (4,469) > 0 — the honest fixture
    for the provisions-in-liabilities branch (prior year was a different 24,475,
    so a period-mixed parse would break the reconciliation).
"""
from pathlib import Path

from company_corpus.registers.dk_fsa_xbrl import parse_fsa_facts
from company_corpus.registers.concepts_dk import map_fsa_facts

_FIXTURES = Path(__file__).parent / "fixtures" / "dk"
MICROB = str(_FIXTURES / "dk_30830725_microB_2025.xml")
STANDARD = str(_FIXTURES / "dk_30560000_2024.xml")
WINDDOWN = str(_FIXTURES / "dk_42566551_2025.xml")
PROVISIONS = str(_FIXTURES / "dk_42710644_provisions.xml")


def _suppressed_keys(result):
    return {k for k, _ in result["suppressed"]}


def _reason(result, key):
    return dict(result["suppressed"]).get(key)


# ===========================================================================
# Task 1 — parser
# ===========================================================================

def test_parse_fsa_facts_microb_2025():
    """parse_fsa_facts on the micro-B 2025 fixture returns correct BS figures."""
    result = parse_fsa_facts(MICROB)

    assert result["currency"] == "DKK"
    assert result["period_end"] == "2025-09-30"

    facts = result["facts"]

    # Balance-sheet figures (instant c4=2025-09-30, not prior-year c5=2024-09-30)
    assert facts["Assets"] == 6744.0
    assert facts["Equity"] == -585256.0
    assert facts["LiabilitiesAndEquity"] == 6744.0
    assert facts["CashAndCashEquivalents"] == 1744.0


def test_parse_current_period_only_excludes_prior_only_facts():
    """REGRESSION (no period-mixing): the parser selects the CURRENT reporting
    period only and never leaks a prior-period-only fact.

    dk_42566551 is a real FY2025 wind-down. fsa:Provisions (15,844) and
    fsa:Revenue (36,536) are tagged ONLY in the 2024 comparative context; the
    2025 current period settled them. The fixed parser therefore OMITS both from
    the current-period facts (never defaulted, never leaked from the prior year),
    and the current row balances on its own.
    """
    result = parse_fsa_facts(WINDDOWN)

    assert result["currency"] == "DKK"
    assert result["period_end"] == "2025-12-31"      # current balance-sheet date

    facts = result["facts"]

    # The heart of the bug: a fact present ONLY in the prior-year context must be
    # ABSENT from the current-period view — not leaked, not defaulted.
    assert "Provisions" not in facts
    assert "Revenue" not in facts

    # Current-period (2025) figures — internally consistent for one period alone.
    assert facts["Assets"] == 77480.0
    assert facts["LiabilitiesAndEquity"] == 77480.0
    assert facts["Equity"] == 77480.0
    assert facts["Assets"] == facts["LiabilitiesAndEquity"]   # balances alone
    assert facts["CashAndCashEquivalents"] == 5462.0
    assert facts["ProfitLoss"] == -2981.0
    assert facts["GrossProfitLoss"] == -2981.0


def test_parse_provisions_selects_current_period_value():
    """The provisions fixture tags fsa:Provisions in BOTH years (current 4,469,
    prior 24,475). The fixed parser returns the CURRENT 4,469 — never the prior
    24,475 — and the current balance sheet reconciles."""
    facts = parse_fsa_facts(PROVISIONS)["facts"]
    assert facts["Provisions"] == 4469.0
    assert facts["Assets"] == 279025.0
    assert facts["LiabilitiesAndEquity"] == 279025.0
    assert facts["Assets"] == facts["LiabilitiesAndEquity"]


# ===========================================================================
# Task 2 — concept pack + NO-FALSE-DATA gate
# ===========================================================================

def test_map_microb_shape_and_currency():
    """The micro-B mapping returns the sibling shape: company basis, DKK, DKK units."""
    r = map_fsa_facts(parse_fsa_facts(MICROB))
    assert r["basis"] == "company"
    assert r["currency"] == "DKK"
    assert r["period_end"] == "2025-09-30"
    assert r["unbalanced"] is False
    # Every emitted value carries unit DKK, a label, and an fsa: tag.
    for key, v in r["values"].items():
        assert v["unit"] == "DKK"
        assert v["label"]
        assert v["tag"].startswith("fsa:") or "derived" in v["tag"]


def test_map_microb_core_values():
    """dk_30830725: assets 6,744 · equity -585,256 · liabilities 592,000 (derived)
    · net_income = fsa:ProfitLoss (-300) · gate holds."""
    r = map_fsa_facts(parse_fsa_facts(MICROB))
    v = r["values"]
    assert v["assets"]["value"] == 6744.0
    assert v["assets"]["tag"] == "fsa:Assets"
    assert v["equity"]["value"] == -585256.0
    assert v["liabilities"]["value"] == 592000.0        # LiabilitiesAndEquity - Equity
    assert "derived" in v["liabilities"]["tag"].lower()
    assert v["net_income"]["value"] == -300.0           # real fsa:ProfitLoss
    assert v["net_income"]["tag"] == "fsa:ProfitLoss"
    assert v["cash"]["value"] == 1744.0


def test_map_microb_revenue_suppressed_gross_profit_is_not_revenue():
    """§32: fsa:Revenue is absent on the micro-B filing -> revenue SUPPRESSED,
    and GrossProfitLoss (-300) is mapped to gross_profit, never to revenue."""
    r = map_fsa_facts(parse_fsa_facts(MICROB))
    assert "revenue" not in r["values"]
    assert "revenue" in _suppressed_keys(r)
    assert r["values"]["gross_profit"]["value"] == -300.0
    assert r["values"]["gross_profit"]["tag"] == "fsa:GrossProfitLoss"
    # The GrossProfitLoss value must not have leaked into revenue.
    assert "revenue" not in r["values"]


def test_map_microb_short_term_debt_reconciles_alone():
    """Micro-B carries only ShorttermLiabilitiesOtherThanProvisions (592,000),
    which alone reconciles to derived liabilities (no provisions) -> emit
    short_term_debt; long_term_debt stays absent (never synthesised)."""
    r = map_fsa_facts(parse_fsa_facts(MICROB))
    assert r["values"]["short_term_debt"]["value"] == 592000.0
    assert r["values"]["short_term_debt"]["tag"] == \
        "fsa:ShorttermLiabilitiesOtherThanProvisions"
    assert "long_term_debt" not in r["values"]
    assert "long_term_debt" in _suppressed_keys(r)
    # Borrowings-by-instrument is never fabricated for class-B filers.
    assert "financial_debt" in _suppressed_keys(r)


def test_map_winddown_current_period_has_no_provisions_or_revenue():
    """dk_42566551 (FY2025 wind-down): the RESTORED real filing's current period.
    Gate holds; derived liabilities are 0 (LiabilitiesAndEquity 77,480 − Equity
    77,480 — every liability was settled); provisions and revenue are ABSENT
    because they belong only to the settled prior year. Absent/zero provisions in
    the current period is the truth — assert it (never the leaked prior 15,844)."""
    r = map_fsa_facts(parse_fsa_facts(WINDDOWN))
    v = r["values"]
    assert r["unbalanced"] is False
    assert r["period_end"] == "2025-12-31"
    assert v["assets"]["value"] == 77480.0
    assert v["equity"]["value"] == 77480.0
    assert v["liabilities"]["value"] == 0.0             # all liabilities settled
    assert v["net_income"]["value"] == -2981.0
    assert v["gross_profit"]["value"] == -2981.0
    # Provisions is a prior-year-only fact -> never emitted for the current period.
    assert "provisions" not in v
    # Revenue tagged only in the prior year -> suppressed (§32 + prior-only).
    assert "revenue" not in v
    assert "revenue" in _suppressed_keys(r)


def test_map_provisions_derived_liabilities_capture_provisions():
    """dk_42710644 (Taxikørsel 011 ApS, FY2025): the CURRENT period carries
    fsa:Provisions (4,469). Derived liabilities (LiabilitiesAndEquity 279,025 −
    Equity 83,118 = 195,907) capture provisions automatically, and provisions is
    also surfaced as its own reported value; gate holds."""
    r = map_fsa_facts(parse_fsa_facts(PROVISIONS))
    v = r["values"]
    assert r["unbalanced"] is False
    assert r["period_end"] == "2025-12-31"
    assert v["assets"]["value"] == 279025.0
    assert v["equity"]["value"] == 83118.0
    assert v["liabilities"]["value"] == 195907.0         # captures provisions 4,469
    assert v["provisions"]["value"] == 4469.0
    assert v["provisions"]["tag"] == "fsa:Provisions"
    # Provisions are inside the derived total liabilities.
    assert v["liabilities"]["value"] >= v["provisions"]["value"]
    assert v["net_income"]["value"] == 63109.0


def test_map_provisions_split_reconciles_with_provisions():
    """The lone short bucket (191,438) + provisions (4,469) reconcile to the
    derived liabilities (195,907) -> emit short_term_debt; provisions stay a
    SEPARATE reported value, NOT counted as debt; no long bucket -> not emitted.

    This reconciliation holds ONLY because provisions is the CURRENT 4,469 (not
    the prior-year 24,475) — a period-mixed parse would break it (191,438 +
    24,475 = 215,913 != 195,907)."""
    r = map_fsa_facts(parse_fsa_facts(PROVISIONS))
    assert r["values"]["short_term_debt"]["value"] == 191438.0
    assert "long_term_debt" not in r["values"]
    # Provisions reported separately, not merged into the debt keys.
    assert r["values"]["provisions"]["value"] == 4469.0


def test_map_revenue_emitted_when_directly_tagged():
    """When fsa:Revenue IS tagged, revenue is emitted from it — the §32
    suppression fires only when fsa:Revenue is absent. Uses a synthetic parsed
    dict: every real DK class-B fixture here presents Bruttoresultat and omits
    fsa:Revenue in the current period, so this branch has no real fixture."""
    parsed = {"period_end": "2025-12-31", "currency": "DKK",
              "facts": {"Assets": 1000.0, "LiabilitiesAndEquity": 1000.0,
                        "Equity": 400.0, "Revenue": 5000.0,
                        "GrossProfitLoss": 250.0}}
    r = map_fsa_facts(parsed)
    assert r["values"]["revenue"]["value"] == 5000.0
    assert r["values"]["revenue"]["tag"] == "fsa:Revenue"
    # GrossProfitLoss keeps its own key and is never conflated with revenue.
    assert r["values"]["gross_profit"]["value"] == 250.0


def test_map_standard_gate_and_net_income():
    """dk_30560000 (FY2025): gate holds; net_income present; real equity/assets."""
    r = map_fsa_facts(parse_fsa_facts(STANDARD))
    v = r["values"]
    assert r["unbalanced"] is False
    assert v["assets"]["value"] == 100000.0
    assert v["equity"]["value"] == -46307.0
    assert v["liabilities"]["value"] == 146307.0         # 100000 - (-46307)
    assert v["net_income"]["value"] == 7994.0
    assert v["short_term_debt"]["value"] == 146307.0     # reconciles alone


def test_map_unbalanced_emits_no_values():
    """Assets != LiabilitiesAndEquity beyond tol -> unbalanced, empty values."""
    parsed = {"period_end": "2024-12-31", "currency": "DKK",
              "facts": {"Assets": 1000.0, "LiabilitiesAndEquity": 1200.0,
                        "Equity": 300.0, "ProfitLoss": 50.0}}
    r = map_fsa_facts(parsed)
    assert r["unbalanced"] is True
    assert r["values"] == {}
    assert "__all__" in _suppressed_keys(r)


def test_map_no_maturity_split_suppresses_debt():
    """A balanced filing with no maturity buckets -> short/long debt suppressed
    (never map a lone total as long_term_debt)."""
    parsed = {"period_end": "2024-12-31", "currency": "DKK",
              "facts": {"Assets": 1000.0, "LiabilitiesAndEquity": 1000.0,
                        "Equity": 400.0}}
    r = map_fsa_facts(parsed)
    assert r["unbalanced"] is False
    assert r["values"]["liabilities"]["value"] == 600.0
    assert "short_term_debt" not in r["values"]
    assert "long_term_debt" not in r["values"]
    assert "short_term_debt" in _suppressed_keys(r)


def test_map_two_bucket_split_reconciles_emits_both():
    """When both correctly-labelled maturity buckets are present and reconcile
    to derived liabilities, emit BOTH short_term_debt and long_term_debt."""
    parsed = {"period_end": "2024-12-31", "currency": "DKK",
              "facts": {"Assets": 1000.0, "LiabilitiesAndEquity": 1000.0,
                        "Equity": 400.0,
                        "ShorttermLiabilitiesOtherThanProvisions": 250.0,
                        "LongtermLiabilitiesOtherThanProvisions": 350.0}}
    r = map_fsa_facts(parsed)
    assert r["values"]["short_term_debt"]["value"] == 250.0
    assert r["values"]["long_term_debt"]["value"] == 350.0
    assert r["values"]["long_term_debt"]["tag"] == \
        "fsa:LongtermLiabilitiesOtherThanProvisions"


def test_map_split_not_reconciling_suppresses_debt():
    """A partial maturity bucket that does NOT reconcile to derived liabilities
    -> suppress the split (no false maturity claim)."""
    parsed = {"period_end": "2024-12-31", "currency": "DKK",
              "facts": {"Assets": 1000.0, "LiabilitiesAndEquity": 1000.0,
                        "Equity": 400.0,
                        "ShorttermLiabilitiesOtherThanProvisions": 100.0}}
    r = map_fsa_facts(parsed)
    assert "short_term_debt" not in r["values"]
    assert "long_term_debt" not in r["values"]


# ===========================================================================
# Task 3 — Virk ESEF bare-XBRL parser (reuses IFRS engine, borrowings leverage)
# ===========================================================================
ESEF_SLICE = str(_FIXTURES / "dk_esef_slice.xml")


def test_parse_virk_esef_xml_returns_flat_shape():
    """parse_virk_esef_xml returns {local_name: [datapoint]} matching oim.flatten_oim_json shape.

    Instant facts (balance-sheet): val, end, unit, tag, label, filed, form, accn — no 'start'.
    Duration facts (P&L): same keys plus 'start'.
    """
    from company_corpus.registers.concepts_dk import parse_virk_esef_xml

    flat = parse_virk_esef_xml(open(ESEF_SLICE, "rb").read())

    # ifrs-full local names present
    assert "Assets" in flat
    assert "Equity" in flat
    assert "Revenue" in flat
    assert "ProfitLoss" in flat
    assert "NoncurrentBorrowings" in flat
    assert "CurrentBorrowings" in flat
    assert "CashAndCashEquivalents" in flat

    # Instant fact: no 'start'; unit = DKK; correct value
    assets_pts = flat["Assets"]
    assert len(assets_pts) == 1          # only no-dim context (dim context excluded)
    pt = assets_pts[0]
    assert pt["val"] == 1_000_000.0
    assert pt["end"] == "2025-12-31"
    assert "start" not in pt
    assert pt["unit"] == "DKK"
    assert pt["tag"] == "Assets"
    assert pt["label"] == "Assets"
    assert "filed" in pt
    assert "form" in pt
    assert "accn" in pt

    # Duration fact: has 'start'
    rev_pts = flat["Revenue"]
    assert len(rev_pts) == 1
    rp = rev_pts[0]
    assert rp["val"] == 2_000_000.0
    assert rp["start"] == "2025-01-01"
    assert rp["end"] == "2025-12-31"
    assert rp["unit"] == "DKK"


def test_parse_virk_esef_xml_skips_dimension_contexts():
    """Contexts with xbrli:scenario children (dimensioned) are excluded — no double-counting."""
    from company_corpus.registers.concepts_dk import parse_virk_esef_xml

    flat = parse_virk_esef_xml(open(ESEF_SLICE, "rb").read())
    # The slice fixture has a dimensioned context with Assets=999999 that must be ignored.
    assert len(flat.get("Assets", [])) == 1


def test_summaries_from_flat_yields_borrowings_debt_to_equity():
    """summaries_from_flat + IFRS_CONCEPTS yields assets/equity/revenue + borrowings-based
    debt_to_equity: (NoncurrentBorrowings + CurrentBorrowings) / Equity."""
    from company_corpus.registers.concepts_dk import parse_virk_esef_xml
    from company_corpus.xbrl import IFRS_CONCEPTS
    from company_corpus.financials import summaries_from_flat

    flat = parse_virk_esef_xml(open(ESEF_SLICE, "rb").read())
    summaries = summaries_from_flat(
        flat, concepts=IFRS_CONCEPTS,
        company="Test DK ESEF Co", company_current="Test DK ESEF Co",
        sic=None,
    )
    assert len(summaries) >= 1
    s = summaries[0]
    assert s.currency == "DKK"

    v = s.values
    assert v["assets"]["value"] == 1_000_000.0
    assert v["equity"]["value"] == 600_000.0
    assert v["revenue"]["value"] == 2_000_000.0
    assert v["net_income"]["value"] == 200_000.0

    # Borrowings-based debt_to_equity via s.derived:
    # total_debt = long_term_debt + short_term_debt = 300,000 + 100,000 = 400,000
    # debt_to_equity = 400,000 / 600,000 ≈ 0.6667
    d = s.derived
    assert "debt_to_equity" in d, f"derived keys: {list(d)}"
    assert abs(d["debt_to_equity"]["value"] - (400_000 / 600_000)) < 0.001


def test_map_dk_esef_balance_gate_holds():
    """map_dk_esef verifies Assets == Equity + Liabilities and returns PeriodSummary list."""
    from company_corpus.registers.concepts_dk import map_dk_esef

    summaries = map_dk_esef(open(ESEF_SLICE, "rb").read())
    assert len(summaries) >= 1
    s = summaries[0]
    v = s.values

    # Balance gate: Assets = 1,000,000 = Equity 600,000 + Liabilities 400,000
    assets = v["assets"]["value"]
    equity = v["equity"]["value"]
    liabilities = v["liabilities"]["value"]
    assert abs(assets - (equity + liabilities)) < max(2.0, 0.005 * abs(assets))

    # Borrowings-based leverage is present in derived
    assert "debt_to_equity" in s.derived
    assert s.currency == "DKK"


def test_map_dk_esef_sector_unknown_no_false_sector_relevant():
    """E-I4 (#68 follow-up): a DK-ESEF filer is identified by LEI only (no SIC/NACE),
    so its sector is UNKNOWN. Its sector-sensitive derived metrics must be stamped
    ``sector_relevant=None`` (honest "unknown"), never a false ``True``; the
    non-sector-sensitive metrics stay ``True``."""
    from company_corpus.registers.concepts_dk import map_dk_esef
    from company_corpus.financials import SECTOR_SENSITIVE

    s = map_dk_esef(open(ESEF_SLICE, "rb").read())[0]
    # Sector is unknown (mirrors the EU pillar), so is_financial declines to assert.
    assert s.sector_known is False
    assert s.is_financial is None

    # A sector-sensitive metric present in the fixture must NOT assert relevance.
    assert "net_debt" in s.derived
    assert s.derived["net_debt"]["sector_relevant"] is None
    # No sector-sensitive derived metric is ever stamped a false True.
    assert all(
        s.derived[k]["sector_relevant"] is None
        for k in s.derived if k in SECTOR_SENSITIVE
    )
    # A non-sector-sensitive metric (leverage vs equity) is still relevant.
    assert s.derived["debt_to_equity"]["sector_relevant"] is True


# ---------------------------------------------------------------------------
# R-I2: DK-ESEF balance gate must DROP (not just warn) unbalanced summaries
# ---------------------------------------------------------------------------

# Minimal ESEF XML with Assets != Equity + Liabilities beyond tolerance.
# Assets = 1_000_000, Equity = 600_000, Liabilities = 100_000
# (sum = 700_000, diff = 300_000 >> tol ≈ 5_000)
_ESEF_UNBALANCED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full"
    xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
  <xbrli:context id="c_instant">
    <xbrli:entity>
      <xbrli:identifier scheme="http://standards.iso.org/iso/17442">529900UNBALLEDTEST001</xbrli:identifier>
    </xbrli:entity>
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>
  <xbrli:context id="c_dur">
    <xbrli:entity>
      <xbrli:identifier scheme="http://standards.iso.org/iso/17442">529900UNBALLEDTEST001</xbrli:identifier>
    </xbrli:entity>
    <xbrli:period>
      <xbrli:startDate>2025-01-01</xbrli:startDate>
      <xbrli:endDate>2025-12-31</xbrli:endDate>
    </xbrli:period>
  </xbrli:context>
  <xbrli:unit id="DKK"><xbrli:measure>iso4217:DKK</xbrli:measure></xbrli:unit>
  <!-- Assets = 1_000_000 but Equity+Liabilities = 700_000 (unbalanced by 300_000) -->
  <ifrs-full:Assets contextRef="c_instant" decimals="0" unitRef="DKK">1000000</ifrs-full:Assets>
  <ifrs-full:Equity contextRef="c_instant" decimals="0" unitRef="DKK">600000</ifrs-full:Equity>
  <ifrs-full:Liabilities contextRef="c_instant" decimals="0" unitRef="DKK">100000</ifrs-full:Liabilities>
  <ifrs-full:Revenue contextRef="c_dur" decimals="0" unitRef="DKK">2000000</ifrs-full:Revenue>
</xbrli:xbrl>
"""


def test_ri2_map_dk_esef_drops_unbalanced_summary():
    """R-I2 regression: map_dk_esef must DROP (not just warn) a summary where
    Assets != Equity+Liabilities beyond tol → returns an empty list, not an 'ok' entry."""
    import warnings
    from company_corpus.registers.concepts_dk import map_dk_esef

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        summaries = map_dk_esef(_ESEF_UNBALANCED_XML)

    # The summary must have been DROPPED — not emitted
    assert summaries == [], (
        f"Expected empty list (unbalanced summary dropped), got {len(summaries)} summaries"
    )
    # The warning must still be raised to aid debugging
    assert any("balance gate" in str(x.message).lower() for x in w), (
        "Expected a balance-gate warning for the unbalanced filing"
    )


def test_ri2_dk_esef_producer_does_not_emit_unbalanced_as_ok(tmp_path):
    """R-I2 regression (producer): build_dk_financials_from_files on an unbalanced
    ESEF file must NOT write a financials JSONL (not emitted as 'ok')."""
    import warnings

    p = tmp_path / "unbal_esef.xml"
    p.write_bytes(_ESEF_UNBALANCED_XML)

    from company_corpus.config import Config
    from company_corpus.registers.financials import build_dk_financials_from_files

    cfg = Config(data_dir=tmp_path)
    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        out = build_dk_financials_from_files([str(p)], config=cfg, write=True)

    # No financials must have been emitted as ok
    assert out["with_financials"] == 0, (
        f"Expected with_financials=0 for unbalanced ESEF, got {out['with_financials']}"
    )
    # No JSONL written for the unbalanced entity
    jsonl_files = list((tmp_path / "financials_register").glob("*.jsonl"))
    assert jsonl_files == [], (
        f"Expected no JSONL for unbalanced entity, found: {jsonl_files}"
    )


# ===========================================================================
# Fix 1 -- dimension detectors must check xbrli:segment, not only xbrli:scenario
# ===========================================================================

# Minimal ESEF XML: real no-dim Assets=1_000_000 + segment-dimensioned Assets=999_999.
# Parser must admit only the no-dim context; the segment-dimensioned fact must be
# excluded so that flat["Assets"] has exactly one datapoint (value 1_000_000).
_ESEF_SEGMENT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2024-03-27/ifrs-full"
    xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
    xmlns:iso4217="http://www.xbrl.org/2003/iso4217">

  <!-- no-dimension instant context -->
  <xbrli:context id="ctx_nodim">
    <xbrli:entity><xbrli:identifier scheme="http://standards.iso.org/iso/17442">529900TESTLEI00000002</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>

  <!-- segment-dimensioned context (ComponentsOfEquityAxis in xbrli:segment) -->
  <xbrli:context id="ctx_seg">
    <xbrli:entity>
      <xbrli:identifier scheme="http://standards.iso.org/iso/17442">529900TESTLEI00000002</xbrli:identifier>
      <xbrli:segment>
        <xbrldi:explicitMember dimension="ifrs-full:ComponentsOfEquityAxis">ifrs-full:IssuedCapitalMember</xbrldi:explicitMember>
      </xbrli:segment>
    </xbrli:entity>
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>

  <xbrli:unit id="DKK"><xbrli:measure>iso4217:DKK</xbrli:measure></xbrli:unit>

  <!-- real top-line Assets in no-dim context -->
  <ifrs-full:Assets contextRef="ctx_nodim" decimals="0" unitRef="DKK">1000000</ifrs-full:Assets>
  <!-- segment-dimensioned Assets: excluded by the parser -->
  <ifrs-full:Assets contextRef="ctx_seg" decimals="0" unitRef="DKK">999999</ifrs-full:Assets>
</xbrli:xbrl>
"""

# Minimal FSA XML: real no-dim fsa:Assets=1_000_000 + segment-dimensioned
# fsa:Assets=999_999. parse_fsa_facts must admit only the no-dim one.
_FSA_SEGMENT_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl
    xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:fsa="http://xbrl.dcca.dk/fsa"
    xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
    xmlns:iso4217="http://www.xbrl.org/2003/iso4217">

  <!-- no-dimension instant context -->
  <xbrli:context id="ctx_nodim">
    <xbrli:entity><xbrli:identifier scheme="http://cvr.dk/orgnr">99999999</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>

  <!-- segment-dimensioned context (some extension dimension via xbrli:segment) -->
  <xbrli:context id="ctx_seg">
    <xbrli:entity>
      <xbrli:identifier scheme="http://cvr.dk/orgnr">99999999</xbrli:identifier>
      <xbrli:segment>
        <xbrldi:explicitMember dimension="fsa:SomeDimension">fsa:SomeMember</xbrldi:explicitMember>
      </xbrli:segment>
    </xbrli:entity>
    <xbrli:period><xbrli:instant>2025-12-31</xbrli:instant></xbrli:period>
  </xbrli:context>

  <xbrli:unit id="DKK"><xbrli:measure>iso4217:DKK</xbrli:measure></xbrli:unit>

  <!-- segment-dimensioned Assets listed FIRST in document order so the test
       is not an accident of first-wins: it must be excluded regardless of order -->
  <fsa:Assets contextRef="ctx_seg" decimals="0" unitRef="DKK">999999</fsa:Assets>
  <!-- real Assets in no-dim context -->
  <fsa:Assets contextRef="ctx_nodim" decimals="0" unitRef="DKK">1000000</fsa:Assets>
</xbrli:xbrl>
"""


def test_parse_virk_esef_xml_excludes_segment_dimensioned_facts():
    """Fix 1 (ESEF): a context dimensioned via xbrli:segment must be treated as
    dimensioned and excluded. The segment-dimensioned Assets=999_999 must not
    appear; only the no-dim Assets=1_000_000 is kept."""
    from company_corpus.registers.concepts_dk import parse_virk_esef_xml

    flat = parse_virk_esef_xml(_ESEF_SEGMENT_XML)
    assets_pts = flat.get("Assets", [])
    assert len(assets_pts) == 1, (
        f"Expected 1 Assets datapoint (no-dim only), got {len(assets_pts)}: "
        f"{[p['val'] for p in assets_pts]}"
    )
    assert assets_pts[0]["val"] == 1_000_000, (
        f"Expected 1_000_000 (no-dim), got {assets_pts[0]['val']} "
        "(segment-dimensioned 999_999 must be excluded)"
    )


def test_parse_fsa_facts_excludes_segment_dimensioned_facts():
    """Fix 1 (FSA): a context dimensioned via xbrli:segment must be excluded.
    The segment-dimensioned fsa:Assets=999_999 must not appear; only the
    no-dim fsa:Assets=1_000_000 is in the facts dict."""
    facts = parse_fsa_facts(_FSA_SEGMENT_XML)["facts"]
    assert "Assets" in facts, "no-dim Assets must be present"
    assert facts["Assets"] == 1_000_000, (
        f"Expected 1_000_000 (no-dim), got {facts['Assets']} "
        "(segment-dimensioned 999_999 must be excluded)"
    )


# ===========================================================================
# Fix 2 -- value unit must follow detected currency, not hardcoded "DKK"
# ===========================================================================

def test_map_fsa_facts_unit_follows_currency_eur():
    """Fix 2: when parse_fsa_facts detects EUR (ARL s.16), every emitted value's
    unit must be 'EUR', not the literal 'DKK'."""
    parsed = {"period_end": "2025-12-31", "currency": "EUR",
              "facts": {"Assets": 500_000.0, "LiabilitiesAndEquity": 500_000.0,
                        "Equity": 200_000.0}}
    r = map_fsa_facts(parsed)
    assert r["currency"] == "EUR"
    for key, v in r["values"].items():
        assert v["unit"] == "EUR", (
            f"values[{key!r}]['unit'] == {v['unit']!r}; expected 'EUR' "
            "(unit must follow detected currency, not hardcoded 'DKK')"
        )


def test_map_fsa_facts_unit_dkk_when_currency_dkk():
    """Fix 2 non-regression: when currency is DKK, unit stays 'DKK'."""
    parsed = {"period_end": "2025-12-31", "currency": "DKK",
              "facts": {"Assets": 1_000.0, "LiabilitiesAndEquity": 1_000.0,
                        "Equity": 400.0}}
    r = map_fsa_facts(parsed)
    for key, v in r["values"].items():
        assert v["unit"] == "DKK", (
            f"values[{key!r}]['unit'] == {v['unit']!r}; expected 'DKK'"
        )


# ===========================================================================
# Task 5 -- keyless Virk acquisition (virk_api.py)
# ===========================================================================

import gzip as _gzip

from company_corpus.registers.virk_api import (
    fetch_virk_document,
    route_document,
    search_virk_filings,
)


# --- stub fetchers -----------------------------------------------------------

class _VirkSearchFetcher:
    """Stub for search_virk_filings: returns a canned ES response."""
    ES_RESPONSE = {
        "hits": {
            "hits": [
                {"_source": {
                    "cvrNummer": 24256790,
                    "offentliggoerelsesTidspunkt": "2024-10-01T00:00:00",
                    "offentliggoerelsestype": "AARSRAPPORT",
                    "regnskab": {
                        "regnskabsperiode": {
                            "startDato": "2023-01-01",
                            "slutDato": "2023-12-31",
                        }
                    },
                    "dokumenter": [
                        {
                            "dokumentType": "AARSRAPPORT",
                            "dokumentMimeType": "application/xml",
                            "dokumentUrl": "http://distribution.virk.dk/doc/123",
                        },
                        {
                            "dokumentType": "AARSRAPPORT",
                            "dokumentMimeType": "application/pdf",
                            "dokumentUrl": "http://distribution.virk.dk/doc/123.pdf",
                        },
                    ],
                }},
                {"_source": {
                    "cvrNummer": 24256790,
                    "offentliggoerelsesTidspunkt": "2023-09-15T00:00:00",
                    "offentliggoerelsestype": "AARSRAPPORT",
                    "regnskab": {
                        "regnskabsperiode": {
                            "startDato": "2022-01-01",
                            "slutDato": "2022-12-31",
                        }
                    },
                    "dokumenter": [],
                }},
            ]
        }
    }

    def post_json(self, url, body, **kw):
        return self.ES_RESPONSE


class _VirkDocFetcher:
    """Stub for fetch_virk_document: returns pre-set bytes."""
    def __init__(self, response_bytes: bytes):
        self._bytes = response_bytes

    def get(self, url, **kw):
        class _Resp:
            def __init__(self, b): self.content = b
        return _Resp(self._bytes)


class _VirkErrorFetcher:
    """Stub that always raises, to test batch-safe [] return."""
    def post_json(self, url, body, **kw):
        raise RuntimeError("network error")


# --- tests -------------------------------------------------------------------

def test_search_virk_filings_returns_sources():
    """search_virk_filings POSTs the ES body and returns hits._source list."""
    results = search_virk_filings("24256790", fetcher=_VirkSearchFetcher())
    assert len(results) == 2
    first = results[0]
    assert first["cvrNummer"] == 24256790
    assert first["offentliggoerelsesTidspunkt"] == "2024-10-01T00:00:00"
    assert len(first["dokumenter"]) == 2
    assert first["regnskab"]["regnskabsperiode"]["slutDato"] == "2023-12-31"


def test_search_virk_filings_batch_safe_on_error():
    """search_virk_filings returns [] on any network error (batch-safe)."""
    results = search_virk_filings("24256790", fetcher=_VirkErrorFetcher())
    assert results == []


def test_fetch_virk_document_gunzips_gzip_response():
    """fetch_virk_document decompresses the payload when magic bytes == 0x1f 0x8b.

    The server sends gzip WITHOUT Content-Encoding: gzip, so the HTTP layer
    does NOT auto-decompress. We compress a real DK fixture in-test, hand the
    raw gz bytes to the stub fetcher, and assert we get back the original XML.
    """
    original_xml = open(MICROB, "rb").read()
    gz_bytes = _gzip.compress(original_xml)
    # Sanity: ensure we created valid gzip with the right magic
    assert gz_bytes[:2] == b"\x1f\x8b"

    result = fetch_virk_document(
        "http://distribution.virk.dk/doc/123",
        fetcher=_VirkDocFetcher(gz_bytes),
    )
    assert result == original_xml


def test_fetch_virk_document_returns_raw_when_not_gzip():
    """fetch_virk_document returns raw bytes when the payload is not gzip."""
    raw_xml = b"<?xml version='1.0'?><root/>"
    result = fetch_virk_document(
        "http://distribution.virk.dk/doc/456",
        fetcher=_VirkDocFetcher(raw_xml),
    )
    assert result == raw_xml


def test_fetch_virk_document_returns_none_on_error():
    """fetch_virk_document returns None on any fetch error (batch-safe)."""
    class _ErrFetcher:
        def get(self, url, **kw): raise RuntimeError("network error")

    result = fetch_virk_document("http://distribution.virk.dk/doc/x",
                                 fetcher=_ErrFetcher())
    assert result is None


def test_route_document_esef_xml():
    """AARSRAPPORT_ESEF + application/xml -> 'esef'."""
    assert route_document({
        "dokumentType": "AARSRAPPORT_ESEF",
        "dokumentMimeType": "application/xml",
    }) == "esef"


def test_route_document_fsa_xml():
    """AARSRAPPORT + application/xml -> 'fsa'."""
    assert route_document({
        "dokumentType": "AARSRAPPORT",
        "dokumentMimeType": "application/xml",
    }) == "fsa"


def test_route_document_pdf_returns_none():
    """AARSRAPPORT + application/pdf -> None (management report PDF)."""
    assert route_document({
        "dokumentType": "AARSRAPPORT",
        "dokumentMimeType": "application/pdf",
    }) is None


def test_route_document_xhtml_returns_none():
    """AARSRAPPORT_ESEF + application/xhtml+xml -> None (iXBRL viewer, not bare XML)."""
    assert route_document({
        "dokumentType": "AARSRAPPORT_ESEF",
        "dokumentMimeType": "application/xhtml+xml",
    }) is None


def test_route_document_unknown_type_returns_none():
    """Unknown dokumentType -> None."""
    assert route_document({
        "dokumentType": "LEDELSESBERETNING",
        "dokumentMimeType": "application/pdf",
    }) is None


# ===========================================================================
# Task 4 — DK identity (CVR / LEI->GLEIF registeredAs)
# ===========================================================================

from company_corpus.registers.identity import _norm_cvr, resolve_register_specs


class _GleifFetcherDK:
    """Minimal GLEIF stub returning one DK entity record."""
    def __init__(self, country, registered_as, name="ACME DENMARK ApS"):
        self._c, self._r, self._n = country, registered_as, name

    def get_json(self, url, **kw):
        return {"data": {"attributes": {"entity": {
            "legalName": {"name": self._n},
            "legalAddress": {"country": self._c},
            "registeredAs": self._r,
        }}}}


def test_norm_cvr_strips_whitespace():
    """_norm_cvr strips surrounding whitespace and keeps 8-digit CVR as string."""
    assert _norm_cvr(" 24256790 ") == "24256790"
    assert _norm_cvr("24256790") == "24256790"
    assert _norm_cvr("  04256790  ") == "04256790"


def test_dk_lei_resolves_via_gleif_registeredas():
    """A DK LEI whose GLEIF country==DK resolves to cvr via registeredAs."""
    r = resolve_register_specs(
        [{"lei": "L_DK1"}],
        fetcher=_GleifFetcherDK("DK", "24256790"),
    )[0]
    assert r["cvr"] == "24256790"
    assert r["lei"] == "L_DK1"
    assert r["country"] == "DK"
    assert r["status"] == "ok"


def test_non_dk_lei_is_unresolved():
    """A LEI whose GLEIF country!=DK must not produce a cvr (no-guess)."""
    r = resolve_register_specs(
        [{"lei": "L_SE1"}],
        fetcher=_GleifFetcherDK("SE", "24256790"),
    )[0]
    assert r.get("cvr") is None
    assert r["status"] == "unresolved"


# ===========================================================================
# Task 6 — DK producer + CLI (build_dk_financials_from_files / build_dk_financials)
# ===========================================================================

import json as _json

from company_corpus.config import Config
from company_corpus.registers.financials import (
    build_dk_financials_from_files,
    build_dk_financials,
)


# ---------------------------------------------------------------------------
# Path B (FSA / DK-GAAP) — from_files
# ---------------------------------------------------------------------------

def test_build_dk_financials_from_files_fsa_writes_jsonl(tmp_path):
    """FSA path (write=True): data/financials_register/30830725.jsonl written;
    rows carry source='erst-fsa', country='DK', basis='company', DKK,
    period_end='2025-09-30', equity=-585256, liabilities=592000."""
    cfg = Config(data_dir=tmp_path)
    out = build_dk_financials_from_files([MICROB], config=cfg, write=True)

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["no_financials"] == 0
    assert out["errors"] == 0

    out_file = tmp_path / "financials_register" / "30830725.jsonl"
    assert out_file.exists(), f"Expected {out_file} to be written"

    rows = [_json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert rows, "JSONL must not be empty"

    for row in rows:
        assert row["source"] == "erst-fsa"
        assert row["country"] == "DK"
        assert row["basis"] == "company"
        assert row["currency"] == "DKK"
        assert row["period_end"] == "2025-09-30"

    reported = {r["concept"]: r["value"] for r in rows if r["kind"] == "reported"}
    assert reported["equity"] == -585256.0
    assert reported["liabilities"] == 592000.0


def test_build_dk_financials_from_files_fsa_dry_run(tmp_path):
    """write=False: no file written, counters correct, paths=[]."""
    cfg = Config(data_dir=tmp_path)
    out = build_dk_financials_from_files([MICROB], config=cfg, write=False)

    assert out["with_financials"] == 1
    assert out["paths"] == []
    out_file = tmp_path / "financials_register" / "30830725.jsonl"
    assert not out_file.exists(), "Dry-run must not write any file"


# Minimal but structurally-valid DK-GAAP FSA instance with BOTH maturity buckets
# reconciling to derived total liabilities (600 = 250 short + 350 long); the real
# micro-B fixtures tag only the short bucket, so none of them yield total_debt.
_FSA_TWO_BUCKET_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"\n'
    '            xmlns:fsa="http://xbrl.dcca.dk/fsa">\n'
    '  <xbrli:context id="c0">\n'
    '    <xbrli:entity>\n'
    '      <xbrli:identifier scheme="http://www.dcca.dk/cvr">12345678</xbrli:identifier>\n'
    '    </xbrli:entity>\n'
    '    <xbrli:period><xbrli:instant>2024-12-31</xbrli:instant></xbrli:period>\n'
    '  </xbrli:context>\n'
    '  <xbrli:unit id="u0"><xbrli:measure>iso4217:DKK</xbrli:measure></xbrli:unit>\n'
    '  <fsa:Assets contextRef="c0" unitRef="u0" decimals="0">1000</fsa:Assets>\n'
    '  <fsa:LiabilitiesAndEquity contextRef="c0" unitRef="u0" decimals="0">1000</fsa:LiabilitiesAndEquity>\n'
    '  <fsa:Equity contextRef="c0" unitRef="u0" decimals="0">400</fsa:Equity>\n'
    '  <fsa:ShorttermLiabilitiesOtherThanProvisions contextRef="c0" unitRef="u0" decimals="0">250</fsa:ShorttermLiabilitiesOtherThanProvisions>\n'
    '  <fsa:LongtermLiabilitiesOtherThanProvisions contextRef="c0" unitRef="u0" decimals="0">350</fsa:LongtermLiabilitiesOtherThanProvisions>\n'
    '</xbrli:xbrl>\n'
)


def test_build_dk_financials_from_files_fsa_leverage_basis_total_liabilities(tmp_path):
    """C1: a DK-GAAP FSA filing whose maturity buckets reconcile yields total_debt
    /debt_to_equity, and those leverage rows carry leverage_basis='total_liabilities'
    (a gearing proxy — DK-GAAP class-B never tags borrowings by instrument)."""
    p = tmp_path / "dk_12345678_twobucket.xml"
    p.write_text(_FSA_TWO_BUCKET_XML)

    cfg = Config(data_dir=tmp_path)
    out = build_dk_financials_from_files([str(p)], config=cfg, write=True)
    assert out["with_financials"] == 1 and out["errors"] == 0

    rows = [
        _json.loads(ln)
        for f in (tmp_path / "financials_register").glob("*.jsonl")
        for ln in f.read_text().splitlines() if ln.strip()
    ]
    lev = [r for r in rows if r["kind"] == "derived"
           and r["concept"] in ("total_debt", "debt_to_equity")]
    assert lev, "expected total_debt/debt_to_equity leverage rows"
    for r in lev:
        assert r["source"] == "erst-fsa"
        assert r["leverage_basis"] == "total_liabilities"
    # FSA entity_id is a CVR (not a LEI) -> the `lei` column stays null (M1 no-op).
    assert all(r["lei"] is None for r in rows)


# ---------------------------------------------------------------------------
# Path A (ESEF / IFRS) — from_files
# ---------------------------------------------------------------------------

def test_build_dk_financials_from_files_esef_counters(tmp_path):
    """ESEF slice → with_financials=1, errors=0, entities=1."""
    cfg = Config(data_dir=tmp_path)
    out = build_dk_financials_from_files([ESEF_SLICE], config=cfg, write=False)

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["errors"] == 0


def test_build_dk_financials_from_files_esef_source_and_dkk(tmp_path):
    """ESEF rows have source='erst-ifrs', country='DK', currency='DKK',
    and borrowings-based debt_to_equity in derived."""
    cfg = Config(data_dir=tmp_path)
    out = build_dk_financials_from_files([ESEF_SLICE], config=cfg, write=True)

    # At least one JSONL file should be written (entity_id from LEI or filename)
    jsonl_files = list((tmp_path / "financials_register").glob("*.jsonl"))
    assert jsonl_files, "Expected at least one JSONL file"

    rows = [
        _json.loads(ln)
        for f in jsonl_files
        for ln in f.read_text().splitlines()
        if ln.strip()
    ]
    assert rows

    for row in rows:
        assert row["source"] == "erst-ifrs"
        assert row["country"] == "DK"
        assert row["currency"] == "DKK"
        # M1: on the ESEF path the entity_id is the filer's LEI (ISO-17442) -> it is
        # surfaced in the `lei` column too, with entity_id kept as-is.
        assert row["entity_id"] == "529900TESTLEI0000001"
        assert row["lei"] == "529900TESTLEI0000001"

    derived_concepts = {r["concept"] for r in rows if r["kind"] == "derived"}
    assert "debt_to_equity" in derived_concepts, (
        f"borrowings-based debt_to_equity missing; derived: {derived_concepts}"
    )
    # C1: DK-ESEF reuses the IFRS engine (NoncurrentBorrowings + CurrentBorrowings)
    # -> borrowings-based leverage, stamped on the leverage-derived rows.
    dte = next(r for r in rows
               if r["kind"] == "derived" and r["concept"] == "debt_to_equity")
    assert dte["leverage_basis"] == "borrowings"


# ---------------------------------------------------------------------------
# Error isolation
# ---------------------------------------------------------------------------

def test_build_dk_financials_from_files_error_isolation(tmp_path):
    """A nonexistent path is counted as error without aborting the batch."""
    cfg = Config(data_dir=tmp_path)
    out = build_dk_financials_from_files(
        ["/nonexistent/dk_30830725_fake.xml", MICROB],
        config=cfg, write=False,
    )
    assert out["errors"] == 1
    assert out["with_financials"] == 1


# ---------------------------------------------------------------------------
# API path (build_dk_financials) — stubbed fetcher
# ---------------------------------------------------------------------------

class _VirkFullStubFetcher:
    """Stub for build_dk_financials: post_json → ES response; get → raw bytes."""

    def __init__(self, cvr: str, xml_bytes: bytes):
        self._cvr = cvr
        self._bytes = xml_bytes

    def post_json(self, url, body, **kw):
        return {
            "hits": {"hits": [{"_source": {
                "cvrNummer": int(self._cvr),
                "offentliggoerelsesTidspunkt": "2025-11-01T00:00:00",
                "offentliggoerelsestype": "AARSRAPPORT",
                "dokumenter": [{
                    "dokumentType": "AARSRAPPORT",
                    "dokumentMimeType": "application/xml",
                    "dokumentUrl": f"http://distribution.virk.dk/doc/{self._cvr}",
                }],
            }}]}
        }

    def get(self, url, **kw):
        class _Resp:
            def __init__(self, b): self.content = b
        return _Resp(self._bytes)

    def get_json(self, url, **kw):
        return {}  # GLEIF lookup not needed for direct cvr path


def test_build_dk_financials_api_stub(tmp_path):
    """API path: stub fetcher returns FSA bytes → writes 30830725.jsonl,
    rows have source='erst-fsa', country='DK'."""
    raw = open(MICROB, "rb").read()
    stub = _VirkFullStubFetcher("30830725", raw)
    cfg = Config(data_dir=tmp_path)

    out = build_dk_financials(
        [{"cvr": "30830725"}],
        fetcher=stub,
        config=cfg,
        write=True,
    )

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["errors"] == 0

    out_file = tmp_path / "financials_register" / "30830725.jsonl"
    assert out_file.exists()
    rows = [_json.loads(ln) for ln in out_file.read_text().splitlines() if ln.strip()]
    assert any(r["source"] == "erst-fsa" for r in rows)
    assert any(r["country"] == "DK" for r in rows)


# ---------------------------------------------------------------------------
# Task 6 fix — ESEF preference when both AARSRAPPORT and AARSRAPPORT_ESEF exist
# ---------------------------------------------------------------------------

_FSA_DOC_URL = "http://distribution.virk.dk/doc/fsa_management_review"
_ESEF_DOC_URL = "http://distribution.virk.dk/doc/esef_ifrs"


class _VirkBothDocsFetcher:
    """Stub for build_dk_financials: a filing carries BOTH an AARSRAPPORT (FSA
    management-review, no balance-sheet facts) AND an AARSRAPPORT_ESEF (real IFRS).

    Tracks whether the FSA URL was ever fetched so tests can assert it was NOT
    accessed after the ESEF-preference fix.
    """

    def __init__(self, cvr: str, esef_bytes: bytes):
        self._cvr = cvr
        self._esef_bytes = esef_bytes
        self.fsa_fetched = False

    def post_json(self, url, body, **kw):
        return {
            "hits": {"hits": [{"_source": {
                "cvrNummer": int(self._cvr),
                "offentliggoerelsesTidspunkt": "2025-11-01T00:00:00",
                "offentliggoerelsestype": "AARSRAPPORT",
                "dokumenter": [
                    {
                        # Listed annual report → management-review XML, no BS/P&L facts.
                        "dokumentType": "AARSRAPPORT",
                        "dokumentMimeType": "application/xml",
                        "dokumentUrl": _FSA_DOC_URL,
                    },
                    {
                        # Real IFRS XBRL for the same filing.
                        "dokumentType": "AARSRAPPORT_ESEF",
                        "dokumentMimeType": "application/xml",
                        "dokumentUrl": _ESEF_DOC_URL,
                    },
                ],
            }}]}
        }

    def get(self, url, **kw):
        class _Resp:
            def __init__(self, b): self.content = b

        if url == _FSA_DOC_URL:
            self.fsa_fetched = True
            # Return a non-FSA/non-ESEF XML that produces no financial facts.
            return _Resp(b"<?xml version='1.0'?><root/>")
        if url == _ESEF_DOC_URL:
            return _Resp(self._esef_bytes)
        return _Resp(b"")

    def get_json(self, url, **kw):
        return {}  # GLEIF lookup not needed for direct cvr path


def test_build_dk_financials_api_prefers_esef_over_fsa_management_review(tmp_path):
    """When a filing has BOTH AARSRAPPORT (FSA, management-review, no financials)
    and AARSRAPPORT_ESEF, build_dk_financials must select the ESEF document first
    and emit source='erst-ifrs'. The FSA URL must not be fetched at all.

    Regression for the scale-validation gap: Novo Nordisk (24256790), Mærsk
    (22756214), Ørsted (36213728), Arla (25313763) all returned no-financials
    because the old code picked AARSRAPPORT before AARSRAPPORT_ESEF.
    """
    esef_bytes = open(ESEF_SLICE, "rb").read()
    stub = _VirkBothDocsFetcher("24256790", esef_bytes)
    cfg = Config(data_dir=tmp_path)

    out = build_dk_financials(
        [{"cvr": "24256790"}],
        fetcher=stub,
        config=cfg,
        write=True,
    )

    assert out["entities"] == 1
    assert out["with_financials"] == 1
    assert out["no_financials"] == 0
    assert out["errors"] == 0

    # The FSA management-review URL must NOT have been accessed.
    assert not stub.fsa_fetched, (
        "ESEF should have been selected first; FSA URL should never be fetched."
    )

    # All rows must carry source='erst-ifrs' — never 'erst-fsa'.
    jsonl_files = list((tmp_path / "financials_register").glob("*.jsonl"))
    assert jsonl_files, "Expected at least one JSONL written"
    rows = [
        _json.loads(ln)
        for f in jsonl_files
        for ln in f.read_text().splitlines()
        if ln.strip()
    ]
    assert any(r["source"] == "erst-ifrs" for r in rows)
    assert not any(r["source"] == "erst-fsa" for r in rows)


# ---------------------------------------------------------------------------
# CLI — --dk-file / --dk-cvr
# ---------------------------------------------------------------------------

def test_cli_dk_file_dry_run(tmp_path):
    """--dk-file dry-run: no file written (default posture)."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--dk-file", MICROB,
    ])
    assert rc == 0
    out_file = tmp_path / "financials_register" / "30830725.jsonl"
    assert not out_file.exists(), "Dry-run must not write any file"


def test_cli_dk_file_write(tmp_path):
    """--dk-file --write: JSONL is written."""
    from company_corpus.cli import main

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--dk-file", MICROB,
        "--write",
    ])
    assert rc == 0
    out_file = tmp_path / "financials_register" / "30830725.jsonl"
    assert out_file.exists()


def test_cli_dk_cvr_dry_run(tmp_path, monkeypatch):
    """--dk-cvr dry-run: build_dk_financials called with write=False; no file written."""
    import company_corpus.cli as _cli
    from company_corpus.cli import main

    calls: list[str] = []

    def _stub(specs, *, fetcher, config, write):
        for s in specs:
            calls.append(s.get("cvr"))
        return {
            "entities": len(specs), "with_financials": 0, "no_financials": len(specs),
            "unbalanced": 0, "errors": 0, "periods": 0, "paths": [],
            "coverage_path": None,
        }

    monkeypatch.setattr(_cli, "build_dk_financials", _stub)

    rc = main([
        "--data-dir", str(tmp_path),
        "register-financials",
        "--dk-cvr", "30830725",
    ])
    assert rc == 0
    assert calls == ["30830725"]
    out_file = tmp_path / "financials_register" / "30830725.jsonl"
    assert not out_file.exists()
