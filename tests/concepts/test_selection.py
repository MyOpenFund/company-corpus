"""The selection on trimmed real filings. Expected values were read in the filings (see the Audit)."""
from decimal import Decimal

import pyarrow as pa

from company_corpus.concepts.selection import recipe, run_selection, selection_sql
from company_corpus.concepts.summaries import FACT_SCHEMA, POSITION_SCHEMA
from tests.concepts.support import millions, pick, selected


def test_marriott_debt_comes_from_the_balance_sheet():
    rows = selected("marriott")
    ltd = pick(rows, "long_term_debt", "2025-12-31")
    assert millions(ltd) == 14995 and ltd["location"] == "statement" and not ltd["conflict"]
    assert millions(pick(rows, "lt_debt_current", "2025-12-31")) == 1209
    total = pick(rows, "debt_total", "2025-12-31")    # the note's total
    assert millions(total) == 16204 and total["location"] == "note"
    assert millions(pick(rows, "long_term_debt_incl_current", "2025-12-31")) == 23    # stray tag
    assert pick(rows, "long_term_debt_lines", "2025-12-31") is None


def test_metlife_revenue_is_the_statement_total():
    r = pick(selected("metlife"), "revenue", "2025-12-31", "annual")
    assert millions(r) == 77084 and r["location"] == "statement" and r["conflict"]


def test_oracle_stray_zero_stays_in_its_own_concept():
    rows = selected("oracle")
    ltd = pick(rows, "long_term_debt", "2022-05-31")
    assert millions(ltd) == 72110 and ltd["original_tag"] == "us-gaap:LongTermNotesAndLoans"
    total = pick(rows, "debt_total", "2022-05-31")
    assert millions(total) == 75859
    assert millions(pick(rows, "long_term_debt_incl_current", "2022-05-31")) == 0
    assert millions(pick(selected("oracle_fy26"), "long_term_debt", "2026-05-31")) == 122342


def test_pepsico_keeps_two_meanings_apart():
    rows = selected("pepsico")
    payables = pick(rows, "payables", "2025-12-27")
    assert millions(payables) == 11704 and payables["location"] == "note"
    bundled = pick(rows, "payables_and_accrued", "2025-12-27")
    assert millions(bundled) == 25903 and bundled["location"] == "statement"


def test_dimension_only_lines_stay_empty():
    assert pick(selected("hershey"), "eps_basic", "2025-12-31", "annual") is None
    assert pick(selected("ford"), "long_term_debt", "2025-12-31") is None


def test_federal_realty_debt_lines_are_summed_and_marked():
    r = pick(selected("federal_realty"), "debt_components", "2025-12-31")
    assert millions(r) == Decimal("4943.1") and r["location"] == "derived"
    assert r["original_tag"] == "us-gaap:UnsecuredDebt+us-gaap:SecuredDebt+us-gaap:NotesPayable"


def test_a_bank_without_operating_income_is_no_error():
    rows = selected("jpmorgan")
    assert pick(rows, "operating_income", "2025-12-31", "annual") is None
    assert millions(pick(rows, "cash", "2025-12-31")) == 21742


def test_a_filing_newer_than_the_bulk_files_borrows_the_previous_positions():
    rows = selected("equinix")
    r = pick(rows, "long_term_debt_incl_current", "2026-09-30")
    assert millions(r) == 22839 and r["location"] == "inferred"
    assert millions(pick(rows, "long_term_debt_lines", "2026-09-30")) == 20976


def test_an_issuer_with_no_positions_at_all_is_unknown_in_dictionary_order():
    r = pick(selected("marriott", statements=False), "long_term_debt", "2025-12-31")
    assert r["location"] == "unknown"
    assert r["original_tag"] == "us-gaap:LongTermDebtAndCapitalLeaseObligations"


def test_liabilities_computed_equals_the_filed_total_with_mezzanine_removed():
    filed = pick(selected("otis"), "liabilities", "2025-12-31")
    assert filed["location"] == "statement"
    computed = pick(selected("otis", drop_tag="Liabilities"), "liabilities", "2025-12-31")
    assert computed["location"] == "derived" and computed["value"] == filed["value"]
    assert computed["original_tag"] == "computed:us-gaap:LiabilitiesAndStockholdersEquity"


def test_no_position_no_computed_total():
    assert pick(selected("otis", statements=False, drop_tag="Liabilities"),
                "liabilities", "2025-12-31") is None


def _fact(tag, value, filing, known, *, end="2024-12-31"):
    from datetime import date, datetime, timezone

    return {"entity_scheme": "cik", "entity_value": "1", "original_tag": f"us-gaap:{tag}",
            "period_start": None, "frequency": "instant", "basis": "consolidated",
            "value": Decimal(value), "unit": "USD", "filing_ref": filing, "source_doc_id": None,
            "known_at": datetime(*known, tzinfo=timezone.utc),
            "valid_date": date.fromisoformat(end), "source_code": "sec"}


def _pos(tag, filing, known, statement="BS"):
    from datetime import datetime, timezone

    return {"entity_scheme": "cik", "entity_value": "1", "filing_ref": filing,
            "statement": statement, "tag": tag, "known_at": datetime(*known, tzinfo=timezone.utc)}


def _run(facts, positions):
    return run_selection(pa.Table.from_pylist(facts, schema=FACT_SCHEMA),
                         pa.Table.from_pylist(positions, schema=POSITION_SCHEMA))


def test_a_later_note_with_another_tag_does_not_beat_an_earlier_statement():
    old, new = (2025, 2, 1), (2025, 8, 1)
    rows = _run([_fact("LongTermNotesAndLoans", 1000, "A", old),
                 _fact("LongTermDebtNoncurrent", 20, "B", new)],
                [_pos("LongTermNotesAndLoans", "A", old), _pos("Assets", "B", new)])
    (r,) = rows
    assert r["value"] == 1000 and r["location"] == "statement" and r["conflict"]


def test_a_later_filing_with_the_same_tag_replaces_the_value():
    old, new = (2025, 2, 1), (2025, 8, 1)
    rows = _run([_fact("LongTermDebtNoncurrent", 1000, "A", old),
                 _fact("LongTermDebtNoncurrent", 900, "B", new)],
                [_pos("LongTermDebtNoncurrent", "A", old), _pos("Assets", "B", new)])
    (r,) = rows
    assert r["value"] == 900 and r["location"] == "note" and not r["conflict"]


def test_a_strong_change_of_the_same_tag_is_flagged_and_still_replaces():
    old, new = (2025, 2, 1), (2025, 8, 1)
    pos = [_pos("LongTermDebtNoncurrent", "A", old), _pos("LongTermDebtNoncurrent", "B", new)]
    (mild,) = _run([_fact("LongTermDebtNoncurrent", 1000, "A", old),
                    _fact("LongTermDebtNoncurrent", 1400, "B", new)], pos)
    assert mild["value"] == 1400 and not mild["conflict"]
    (strong,) = _run([_fact("LongTermDebtNoncurrent", 1000, "A", old),
                      _fact("LongTermDebtNoncurrent", 1, "B", new)], pos)
    assert strong["value"] == 1 and strong["conflict"]


def test_real_positions_replace_borrowed_ones_as_soon_as_they_exist():
    old, new = (2025, 2, 1), (2025, 8, 1)
    facts = [_fact("LongTermNotesAndLoans", 1000, "A", old), _fact("LongTermNotesAndLoans", 1100, "B", new)]
    before = [_pos("LongTermNotesAndLoans", "A", old)]
    (r,) = _run(facts, before)
    assert r["value"] == 1100 and r["location"] == "inferred"
    (r,) = _run(facts, [*before, _pos("LongTermNotesAndLoans", "B", new)])
    assert r["value"] == 1100 and r["location"] == "statement"


def test_real_positions_can_change_the_borrowed_choice():
    old, new = (2025, 2, 1), (2025, 8, 1)
    facts = [_fact("LongTermNotesAndLoans", 1000, "B", new), _fact("LongTermDebtAndCapitalLeaseObligations", 40, "B", new)]
    before = [_pos("LongTermNotesAndLoans", "A", old)]
    (r,) = _run(facts, before)
    assert r["value"] == 1000 and r["location"] == "inferred"
    (r,) = _run(facts, [*before, _pos("LongTermDebtAndCapitalLeaseObligations", "B", new)])
    assert r["value"] == 40 and r["location"] == "statement"


def test_a_statement_only_tag_in_a_note_is_no_candidate():
    k = (2025, 2, 1)
    assert _run([_fact("CashAndDueFromBanks", 5, "A", k)], [_pos("Assets", "A", k)]) == []


def test_conflict_tolerance_and_a_zero_value():
    k = (2025, 2, 1)
    near = _run([_fact("LongTermDebtNoncurrent", 1000, "A", k), _fact("LongTermDebtAndCapitalLeaseObligations", 1005, "A", k)], [])
    assert not near[0]["conflict"]
    far = _run([_fact("LongTermDebtNoncurrent", 1000, "A", k), _fact("LongTermDebtAndCapitalLeaseObligations", 1020, "A", k)], [])
    assert far[0]["conflict"]
    zero = _run([_fact("LongTermDebtNoncurrent", 0, "A", k), _fact("LongTermDebtAndCapitalLeaseObligations", 5, "A", k)], [])
    assert zero[0]["conflict"]


def test_sum_uses_only_the_latest_filing_with_statement_lines():
    old, new = (2025, 2, 1), (2025, 8, 1)
    rows = _run([_fact("SeniorNotes", 100, "A", old), _fact("SecuredDebt", 50, "B", new)],
                [_pos("SeniorNotes", "A", old), _pos("SecuredDebt", "B", new)])
    (r,) = rows
    assert r["value"] == 50 and r["location"] == "derived"


def test_empty_inputs_give_no_row():
    assert _run([], []) == []


def test_the_sql_is_one_statement_and_the_recipe_names_its_inputs():
    import duckdb

    assert len(duckdb.connect().extract_statements(selection_sql())) == 1


def _for(entity, rows):
    return [{**r, "entity_value": entity} for r in rows]


def _two_entities(parent_pos, sub_pos):
    k = (2025, 2, 1)
    tags = [("SeniorNotes", 100), ("SecuredDebt", 50)]
    facts = [*_for("1", [_fact(t, v, "A", k) for t, v in tags]),
             *_for("2", [_fact(t, v * 2, "A", k) for t, v in tags])]
    pos = [*_for("1", [_pos(t, "A", k) for t, _ in tags] if parent_pos else []),
           *_for("2", [_pos(t, "A", k) for t, _ in tags] if sub_pos else [])]
    return {r["entity_value"]: r for r in _run(facts, pos) if r["concept"] == "debt_components"}


def test_two_entities_sharing_an_accession_are_not_doubled_when_both_borrow():
    old, new = (2025, 2, 1), (2025, 8, 1)
    tags = [("SeniorNotes", 100), ("SecuredDebt", 50)]
    facts = [*_for("1", [_fact(t, v, "B", new) for t, v in tags]),
             *_for("2", [_fact(t, v * 2, "B", new) for t, v in tags])]
    pos = [*_for("1", [_pos(t, "A", old) for t, _ in tags]),
           *_for("2", [_pos(t, "A", old) for t, _ in tags])]
    rows = {r["entity_value"]: r for r in _run(facts, pos) if r["concept"] == "debt_components"}
    assert rows["1"]["value"] == 150 and rows["2"]["value"] == 300
    assert rows["1"]["original_tag"] == "us-gaap:SeniorNotes+us-gaap:SecuredDebt"
    assert _two_entities(False, False) == {}


def test_two_entities_sharing_an_accession_with_positions_for_both():
    rows = _two_entities(True, True)
    assert rows["1"]["value"] == 150 and rows["2"]["value"] == 300
    assert rows["1"]["original_tag"].count("us-gaap:SeniorNotes") == 1


def test_positions_under_one_entity_only_are_decided_per_entity():
    rows = _two_entities(True, False)
    assert rows["1"]["value"] == 150 and rows["1"]["location"] == "derived"
    # the subsidiary has no statement lines of its own: no tier-0 candidate, so no sum
    assert "2" not in rows


def test_a_borrowed_filing_never_crosses_entities():
    old, new = (2025, 2, 1), (2025, 8, 1)
    facts = [*_for("1", [_fact("LongTermNotesAndLoans", 1000, "B", new)]),
             *_for("2", [_fact("LongTermNotesAndLoans", 7, "B", new)])]
    rows = {r["entity_value"]: r for r in _run(facts, _for("1", [_pos("LongTermNotesAndLoans", "A", old)]))}
    assert rows["1"]["location"] == "inferred" and rows["2"]["location"] == "unknown"


def test_huge_values_cannot_overflow_the_tolerance_checks():
    k = (2025, 2, 1)
    rows = _run([_fact("LongTermDebtNoncurrent", 10**31, "A", k),
                 _fact("LongTermDebtAndCapitalLeaseObligations", 5, "A", k)], [])
    assert rows[0]["conflict"]
    opposite = _run([_fact("LongTermDebtNoncurrent", 9 * 10**31, "A", k),
                     _fact("LongTermDebtAndCapitalLeaseObligations", -9 * 10**31, "A", k)], [])
    assert opposite[0]["conflict"]
    old, new = (2025, 2, 1), (2025, 8, 1)
    restated = _run([_fact("LongTermDebtNoncurrent", 9 * 10**31, "A", old),
                     _fact("LongTermDebtNoncurrent", -9 * 10**31, "B", new)], [])
    assert restated[0]["conflict"]


def test_cash_only_counts_on_the_balance_sheet():
    k = (2025, 2, 1)
    assert _run([_fact("Cash", 250, "A", k)], [_pos("Assets", "A", k)]) == []
    (on_bs,) = _run([_fact("Cash", 250, "A", k)], [_pos("Cash", "A", k)])
    assert on_bs["concept"] == "cash" and on_bs["value"] == 250


def test_net_income_prefers_the_parent_line_then_the_common_line():
    k = (2025, 2, 1)
    both = [_fact("NetIncomeLoss", 100, "A", k),
            _fact("NetIncomeLossAvailableToCommonStockholdersBasic", 90, "A", k)]
    pos = [_pos(t, "A", k, "IS") for t in ("NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic")]
    (r,) = _run(both, pos)
    assert r["original_tag"] == "us-gaap:NetIncomeLoss"
    (r,) = _run(both[1:], pos[1:])
    assert r["original_tag"] == "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic"


def test_debt_lines_are_summed_when_on_the_statement_only():
    k = (2025, 2, 1)
    facts = [_fact("LongTermLineOfCredit", 360, "A", k), _fact("SeniorLongTermNotes", 500, "A", k),
             _fact("LongTermLoansPayable", 7, "A", k)]
    (r,) = _run(facts, [_pos("LongTermLineOfCredit", "A", k), _pos("SeniorLongTermNotes", "A", k)])
    assert r["concept"] == "long_term_debt_lines" and r["value"] == 860 and r["location"] == "derived"
