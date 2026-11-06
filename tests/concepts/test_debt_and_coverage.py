import pytest

from company_corpus.concepts.coverage import coverage, format_coverage
from company_corpus.financials import attach_ttm_metrics, build_period_summaries, compute_derived
from tests.concepts.support import fixture, selected


def _v(**kw):
    return {k: {"value": v, "unit": "USD", "label": k} for k, v in kw.items()}


@pytest.mark.parametrize("values, expected", [
    (dict(long_term_debt=100, lt_debt_current=10, short_term_debt=5), 115),
    (dict(long_term_debt=100, debt_current_total=30), 130),
    (dict(long_term_debt=100, debt_current_total=30, short_term_debt=5), 105),
    (dict(long_term_debt=100, debt_total=999), 100),
    (dict(debt_total=500, lt_debt_current=10), 500),
    (dict(debt_components=70, lt_debt_current=10), 70),
    (dict(debt_total=500, debt_components=70), 500),
    (dict(short_term_debt=5), 5),
    (dict(lt_debt_current=10, short_term_debt=5), 15),
    (dict(debt_current_total=30), 30),
])
def test_total_debt_order(values, expected):
    assert compute_derived(_v(**values), debt_rule="concepts")["total_debt"]["value"] == expected


def test_a_debt_free_filer_has_no_total_debt():
    assert "total_debt" not in compute_derived(_v(assets=10), debt_rule="concepts")


def test_the_default_rule_still_reads_tags_for_the_other_pillars():
    values = {"long_term_debt": {"value": 1000, "unit": "EUR", "label": "x", "tag": "Borrowings"},
              "short_term_debt": {"value": 300, "unit": "EUR", "label": "x", "tag": "CurrentBorrowings"}}
    assert compute_derived(values)["total_debt"]["value"] == 1000


def test_net_debt_uses_bundled_cash_when_cash_is_empty():
    d = compute_derived(_v(long_term_debt=100, cash_and_st_investments=30, short_term_investments=7),
                        debt_rule="concepts")
    assert d["net_debt"]["value"] == 70


def test_one_filing_one_concept_and_a_tag_only_as_comparative():
    def rev(val, start, end):
        return {"val": val, "start": start, "end": end, "accn": "A", "form": "10-K", "filed": "2025-02-01"}

    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        rev(10, "2024-01-01", "2024-12-31"), rev(9, "2023-01-01", "2023-12-31")]}}}}}
    summaries = build_period_summaries(facts, company="X", company_current="X")
    attach_ttm_metrics(facts, summaries)
    assert [(s.fy, s.values["revenue"]["value"], s.values["revenue"]["location"]) for s in summaries] == [
        (2024, 10, "unknown"), (2023, 9, "unknown")]
    assert not summaries[0].derived.get("total_debt")


def test_marriott_total_debt_end_to_end():
    fx = fixture("marriott")
    (fy,) = [s for s in build_period_summaries(fx["facts"], company="M", company_current="M",
                                               statements=fx["statements"])
             if s.frequency == "annual" and s.period_end.isoformat() == "2025-12-31"]
    assert fy.values["long_term_debt"]["tag"] == "LongTermDebtAndCapitalLeaseObligations"
    assert fy.derived["total_debt"]["value"] == 16204000000


def test_xbrl_reports_a_failed_statements_refresh_and_still_builds(config, xbrl_fetcher, monkeypatch):
    import company_corpus.pipeline as pipeline
    from company_corpus.opendata.statements import StatementsReport

    monkeypatch.setattr(pipeline, "refresh_statements", lambda *a, **kw: StatementsReport(
        write=True, errors=["2026q3: expected and missing (404)"]))
    rep = pipeline.fetch_financials(["320193"], config=config, fetcher=xbrl_fetcher, dry_run=False)
    assert rep.periods > 0 and rep.errors[0]["source"] == "sec_statements"


def _on_statement(name):
    return {(s["accn"], s["tag"]) for s in fixture(name)["statements"]}


def test_a_chain_tag_on_the_statement_without_a_value_is_counted():
    c = coverage(selected("hershey"), _on_statement("hershey"))
    assert c["coverage:eps_basic:absent"] == 1 and c["coverage:eps_basic:on_statement_no_value"] == 1
    assert c["coverage:assets:statement"] == 1
    assert c["coverage:rnd_expense:on_statement_no_value"] == 0
    ford = coverage(selected("ford"), _on_statement("ford"))
    assert ford["coverage:long_term_debt:on_statement_no_value"] == 1


def test_no_annual_period_is_counted_not_a_crash():
    assert coverage([], set()) == {"coverage:no_annual_period": 1}


def test_format_prints_one_line_per_concept_in_dictionary_order():
    lines = list(format_coverage(coverage(selected("marriott"), _on_statement("marriott"))))
    assert lines[0].startswith("coverage revenue: ") and any(" derived=1" in x for x in lines)
