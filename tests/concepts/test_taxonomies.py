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
    assert r["review_status"] == "reviewed" and r["dictionary_version"] == "2.0.0"


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
