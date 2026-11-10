"""Two taxonomies in one selection (chantier 10 spec §5)."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pyarrow as pa

from company_corpus.concepts.selection import run_selection
from company_corpus.concepts.summaries import FACT_SCHEMA, POSITION_SCHEMA
from tests.concepts.tables import with_test_ifrs

D = with_test_ifrs()
OLD, NEW = (2023, 3, 1), (2024, 3, 1)


def _fact(full, value, filing, known, *, end="2022-12-31", start=None, unit="EUR"):
    return {"entity_scheme": "cik", "entity_value": "1", "original_tag": full,
            "period_start": date.fromisoformat(start) if start else None,
            "frequency": "annual" if start else "instant", "basis": "consolidated",
            "value": Decimal(value), "unit": unit, "filing_ref": filing, "source_doc_id": None,
            "known_at": datetime(*known, tzinfo=timezone.utc),
            "valid_date": date.fromisoformat(end), "source_code": "sec"}


def _pos(tag, filing, known, version, statement="BS"):
    return {"entity_scheme": "cik", "entity_value": "1", "filing_ref": filing,
            "statement": statement, "tag": tag, "tag_version": version,
            "known_at": datetime(*known, tzinfo=timezone.utc)}


def _run(facts, positions):
    return run_selection(pa.Table.from_pylist(facts, schema=FACT_SCHEMA),
                         pa.Table.from_pylist(positions, schema=POSITION_SCHEMA), D)


def _concept(rows, key):
    found = [r for r in rows if r["concept"] == key]
    assert len(found) <= 1, found
    return found[0] if found else None


def test_an_ifrs_fact_is_on_the_statement_only_through_an_ifrs_position():
    facts = [_fact("ifrs-full:Assets", 50, "A", OLD)]
    on = _concept(_run(facts, [_pos("Assets", "A", OLD, "ifrs/2022")]), "assets")
    assert on["location"] == "statement" and on["on_statement"]
    off = _concept(_run(facts, [_pos("Assets", "A", OLD, "us-gaap/2022")]), "assets")
    assert off["location"] == "note" and not off["on_statement"]


def test_an_extension_position_never_places_a_dictionary_tag():
    facts = [_fact("ifrs-full:Assets", 50, "A", OLD)]
    r = _concept(_run(facts, [_pos("Assets", "A", OLD, "0000000001-23-000001")]), "assets")
    assert r["location"] == "note"


def test_a_filer_that_switched_gets_one_row_from_the_latest_filing():
    facts = [_fact("ifrs-full:Assets", 50, "A", OLD),
             _fact("us-gaap:Assets", 52, "B", NEW, unit="USD")]
    positions = [_pos("Assets", "A", OLD, "ifrs/2022"), _pos("Assets", "B", NEW, "us-gaap/2023")]
    r = _concept(_run(facts, positions), "assets")
    assert r["original_tag"] == "us-gaap:Assets" and r["value"] == 52


def test_first_in_one_taxonomy_and_sum_in_the_other_still_give_one_row():
    # debt_components is "sum" in the us-gaap table and "first" in the test IFRS table.
    facts = [_fact("ifrs-full:Borrowings", 30, "A", OLD),
             _fact("us-gaap:UnsecuredDebt", 10, "B", NEW, unit="USD"),
             _fact("us-gaap:SecuredDebt", 15, "B", NEW, unit="USD")]
    positions = [_pos("Borrowings", "A", OLD, "ifrs/2022"),
                 _pos("UnsecuredDebt", "B", NEW, "us-gaap/2023"),
                 _pos("SecuredDebt", "B", NEW, "us-gaap/2023")]
    r = _concept(_run(facts, positions), "debt_components")
    assert r["value"] == 25 and r["location"] == "derived"


def test_older_ifrs_only_periods_keep_their_ifrs_value():
    facts = [_fact("ifrs-full:Assets", 40, "A", OLD, end="2021-12-31"),
             _fact("us-gaap:Assets", 52, "B", NEW, unit="USD")]
    rows = _run(facts, [_pos("Assets", "A", OLD, "ifrs/2022"), _pos("Assets", "B", NEW, "us-gaap/2023")])
    by_day = {r["valid_date"].isoformat(): r["original_tag"] for r in rows if r["concept"] == "assets"}
    assert by_day == {"2021-12-31": "ifrs-full:Assets", "2022-12-31": "us-gaap:Assets"}


def test_a_concept_the_ifrs_table_omits_stays_empty():
    facts = [_fact("ifrs-full:GrossProfit", 9, "A", OLD, start="2022-01-01"),
             _fact("ifrs-full:Revenue", 90, "A", OLD, start="2022-01-01")]
    rows = _run(facts, [_pos("Revenue", "A", OLD, "ifrs/2022", "IS")])
    assert _concept(rows, "gross_profit") is None and _concept(rows, "revenue")["value"] == 90


def test_no_computed_total_in_a_taxonomy_without_one():
    facts = [_fact("ifrs-full:EquityAttributableToOwnersOfParent", 20, "A", OLD)]
    rows = _run(facts, [_pos("EquityAttributableToOwnersOfParent", "A", OLD, "ifrs/2022")])
    assert _concept(rows, "liabilities") is None


def test_in_one_filing_the_taxonomy_on_the_statement_answers():
    facts = [_fact("ifrs-full:Assets", 50, "A", OLD), _fact("us-gaap:Assets", 49, "A", OLD)]
    r = _concept(_run(facts, [_pos("Assets", "A", OLD, "ifrs/2022")]), "assets")
    assert r["original_tag"] == "ifrs-full:Assets"


def test_review_status_comes_from_the_answering_table():
    facts = [_fact("ifrs-full:Assets", 50, "A", OLD)]
    r = _concept(_run(facts, [_pos("Assets", "A", OLD, "ifrs/2022")]), "assets")
    assert r["review_status"] == "reviewed" and r["dictionary_version"] == "2.1.0"


def test_one_filing_with_two_versions_of_a_tag_counts_the_fact_once():
    facts = [_fact("us-gaap:UnsecuredDebt", 10, "B", NEW, unit="USD"),
             _fact("us-gaap:SecuredDebt", 15, "B", NEW, unit="USD")]
    positions = [_pos("UnsecuredDebt", "B", NEW, "us-gaap/2023"),
                 _pos("UnsecuredDebt", "B", NEW, "us-gaap/2024"),
                 _pos("SecuredDebt", "B", NEW, "us-gaap/2024")]
    r = _concept(_run(facts, positions), "debt_components")
    assert r["value"] == 25 and r["original_tag"] == "us-gaap:UnsecuredDebt+us-gaap:SecuredDebt"


def test_a_computed_total_reads_one_taxonomy():
    facts = [_fact("ifrs-full:EquityAttributableToOwnersOfParent", 20, "A", OLD),
             _fact("us-gaap:LiabilitiesAndStockholdersEquity", 100, "A", OLD, unit="USD")]
    positions = [_pos("EquityAttributableToOwnersOfParent", "A", OLD, "ifrs/2024"),
                 _pos("LiabilitiesAndStockholdersEquity", "A", OLD, "us-gaap/2024")]
    rows = _run(facts, positions)
    assert _concept(rows, "liabilities") is None
    assert _concept(rows, "equity")["original_tag"] == "ifrs-full:EquityAttributableToOwnersOfParent"


def test_a_position_under_another_taxonomy_does_not_lend_its_statement():
    # Same filing: Assets sits on CF under the IFRS version and on BS under another version.
    facts = [_fact("ifrs-full:Assets", 50, "A", OLD)]
    for other in ("us-gaap/2022", "0000000001-23-000001"):
        positions = [_pos("Assets", "A", OLD, "ifrs/2022", "CF"), _pos("Assets", "A", OLD, other, "BS")]
        r = _concept(_run(facts, positions), "assets")
        assert r["location"] == "statement" and not r["on_statement"], other


def test_a_sum_concepts_candidates_are_its_statement_lines():
    facts = [_fact("ifrs-full:Borrowings", 30, "A", OLD),
             _fact("us-gaap:UnsecuredDebt", 10, "B", NEW, unit="USD")]
    positions = [_pos("Borrowings", "A", OLD, "ifrs/2022"), _pos("Assets", "B", NEW, "us-gaap/2023")]
    r = _concept(_run(facts, positions), "debt_components")
    assert r["value"] == 30 and r["original_tag"] == "ifrs-full:Borrowings"


from company_corpus.concepts.summaries import build_summaries, f1_coverage  # noqa: E402
from tests.concepts.tables import companyfacts  # noqa: E402

A1 = "0000000001-25-000001"


def _cf_point(unit, val, end, *, start=None):
    p = {"unit": unit, "val": val, "end": end, "accn": A1, "form": "20-F", "filed": "2025-03-01"}
    if start:
        p["start"] = start
    return p


def _gbp_filer(liabilities_version="ifrs/2024"):
    facts = companyfacts({
        "ifrs-full:Revenue": [_cf_point("GBP", 14000, "2024-12-31", start="2024-01-01")],
        "ifrs-full:ProfitLossAttributableToOwnersOfParent": [
            _cf_point("GBP", 4000, "2024-12-31", start="2024-01-01")],
        "ifrs-full:Assets": [_cf_point("GBP", 700000, "2024-12-31")]})
    lines = (("IS", "Revenue", "ifrs/2024"),
             ("IS", "ProfitLossAttributableToOwnersOfParent", "ifrs/2024"),
             ("BS", "Assets", "ifrs/2024"), ("BS", "Liabilities", liabilities_version))
    statements = [{"accn": A1, "form": "20-F", "period": "20241231", "filed": "20250301",
                   "accepted": "2025-03-01 10:00:00.0", "statement": st, "tag": tag,
                   "version": version, "plabel": tag, "report": 2, "line": n}
                  for n, (st, tag, version) in enumerate(lines)]
    return facts, statements


def test_an_ifrs_filer_gets_an_f1_summary_in_its_currency():
    # Guard: Tasks 2 and 3 already make this pass.
    facts, statements = _gbp_filer()
    (s,) = build_summaries(facts, company="T", company_current="T", statements=statements,
                           dictionary=D)
    assert s.currency == "GBP" and s.frequency == "annual"
    assert s.values["revenue"]["value"] == 14000 and s.values["revenue"]["unit"] == "GBP"
    assert s.values["revenue"]["tag"] == "Revenue" and s.values["revenue"]["location"] == "statement"
    assert s.values["net_income"]["value"] == 4000


def test_a_dictionary_line_without_a_value_is_reported():
    counts = f1_coverage(*_gbp_filer(), D)
    assert counts["coverage:revenue:statement"] == 1
    assert counts["coverage:liabilities:on_statement_no_value"] == 1
    assert counts["coverage:gross_profit:on_statement_no_value"] == 0


def test_an_extension_line_is_not_a_dictionary_line():
    # Same bare name, but the version is an accession number: an extension element.
    counts = f1_coverage(*_gbp_filer(liabilities_version="0000000001-25-000001"), D)
    assert counts["coverage:liabilities:on_statement_no_value"] == 0


def test_pin_note_value_from_another_taxonomy_answers_when_statement_only_tag_is_off_statement():
    """Pins today's behaviour (forbidding it would need a spec change). ifrs-full:Revenue is
    statement-only for revenue; in the one filing it is located but off the statement, and a stray
    us-gaap:Revenues sits in a note: the us-gaap note value answers."""
    import copy

    from company_corpus.concepts.dictionary import parse_dictionary
    from tests.concepts.tables import TEST_IFRS_TABLE, _read
    table = copy.deepcopy(TEST_IFRS_TABLE)
    table["concepts"]["revenue"].update(tags=[], statement_only_tags=["Revenue"])
    d = parse_dictionary(_read("concepts.json"),
                         [("us_gaap", _read("tags/us_gaap.json")), ("ifrs_full", table)])
    facts = [_fact("ifrs-full:Revenue", 90, "A", OLD, start="2022-01-01"),
             _fact("us-gaap:Revenues", 77, "A", OLD, start="2022-01-01", unit="USD")]
    positions = [_pos("Revenue", "A", OLD, "ifrs/2022", "CF")]  # off the statement (cash-flow page)
    rows = run_selection(pa.Table.from_pylist(facts, schema=FACT_SCHEMA),
                         pa.Table.from_pylist(positions, schema=POSITION_SCHEMA), d)
    r = _concept(rows, "revenue")
    assert r["original_tag"] == "us-gaap:Revenues" and r["value"] == 77 and r["location"] == "note"


def test_a_currency_switcher_today_keeps_only_the_issuer_wide_currency_period():
    """IFRS 20-F (EUR) FY2022, then US GAAP 10-K (USD) FY2023. F1 has ONE issuer-wide currency
    (reporting_currency of all flattened facts) and drops monetary points in any other currency, so
    a currency switcher does NOT get one summary per period today: this pins that limitation, it is
    not the desired behaviour."""
    a2, a3 = "0000000001-23-000001", "0000000001-24-000001"
    facts = companyfacts({
        "ifrs-full:Assets": [{"unit": "EUR", "val": 50, "end": "2022-12-31", "accn": a2,
                              "form": "20-F", "filed": "2023-03-01"}],
        "ifrs-full:Revenue": [{"unit": "EUR", "val": 9, "start": "2022-01-01", "end": "2022-12-31",
                               "accn": a2, "form": "20-F", "filed": "2023-03-01"}],
        "us-gaap:Revenues": [{"unit": "USD", "val": 11, "start": "2023-01-01", "end": "2023-12-31",
                              "accn": a3, "form": "10-K", "filed": "2024-03-01"}],
        "us-gaap:Assets": [{"unit": "USD", "val": 60, "end": "2023-12-31", "accn": a3,
                            "form": "10-K", "filed": "2024-03-01"}]})
    statements = [
        {"accn": a2, "form": "20-F", "period": "20221231", "filed": "20230301",
         "accepted": "2023-03-01 10:00:00.0", "statement": "BS", "tag": "Assets",
         "version": "ifrs/2023", "plabel": "Assets", "report": 2, "line": 0},
        {"accn": a3, "form": "10-K", "period": "20231231", "filed": "20240301",
         "accepted": "2024-03-01 10:00:00.0", "statement": "BS", "tag": "Assets",
         "version": "us-gaap/2024", "plabel": "Assets", "report": 2, "line": 0},
        {"accn": a2, "form": "20-F", "period": "20221231", "filed": "20230301",
         "accepted": "2023-03-01 10:00:00.0", "statement": "IS", "tag": "Revenue",
         "version": "ifrs/2023", "plabel": "Revenue", "report": 3, "line": 0},
        {"accn": a3, "form": "10-K", "period": "20231231", "filed": "20240301",
         "accepted": "2024-03-01 10:00:00.0", "statement": "IS", "tag": "Revenues",
         "version": "us-gaap/2024", "plabel": "Revenues", "report": 3, "line": 0}]
    out = build_summaries(facts, company="T", company_current="T", statements=statements,
                          dictionary=D)
    annual = [s for s in out if s.frequency == "annual"]
    # Today: one issuer-wide currency (the most frequent unit; ties go to USD). Here EUR and USD tie
    # at 2 points each, so USD is the issuer currency and the EUR-only FY2022 period gets NO summary.
    assert [(s.period_end.year, s.currency) for s in annual] == [(2023, "USD")]
    assert annual[0].values["assets"]["unit"] == "USD"
