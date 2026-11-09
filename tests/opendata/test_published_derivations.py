"""Golden test: the shared derivation tables that published fundamentals rows depend on.

The whole form index behind ``taxonomy.from_edgar_form`` (which form
gives which ``source_doc_id`` family), the ``source_doc_id`` recipe, the currency anchor tags
and the ISO currency pattern are shared with the F1 engine or frozen by it, but
``store-batches`` publishes rows derived from them. A fundamentals row's ``known_at`` is the filing's acceptance time, which
never moves, and a refresh of the submissions archive does not change it.

Changing one of these tables therefore alters rows already published at the same
``known_at``: the publisher quarantines the batch as ``conflicting_republish``.

The concept dictionary is not one of them: rows are as filed, keyed by tag, so changing it changes
no published row (tested below). A new tag brings new rows.
Chantier 10 (2026-11-09) rewrote the anchor tags as full tags and added the ifrs-full ones. The
us-gaap four keep their meaning (a US filing has no ifrs-full fact, so no vote moves); this was
checked against every published issuer on the real corpus before merging.
The expected values are in ``published_derivations.json`` next to this module.
"""
import json
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from company_corpus import financials, taxonomy
from company_corpus.concepts.dictionary import parse_dictionary
from company_corpus.config import DEFAULT_CURRENCY_ANCHOR_TAGS, Config
from company_corpus.models import FilingRecord
from company_corpus.opendata.fundamentals import fundamentals_rows
from company_corpus.taxonomy import from_edgar_form
from tests.concepts.support import fixture

GOLDEN = json.loads((Path(__file__).parent / "published_derivations.json").read_text("utf-8"))

WHY = ("Changing this alters rows already published at the same known_at, so the publisher "
       "will quarantine them (conflicting_republish).")


def _rows(dictionary):
    fx = fixture("marriott")
    res = fundamentals_rows(fx["cik"], fx["facts"], acceptance={}, manifest_doc_ids=set(),
                            config=Config(), now=datetime(2026, 11, 6, tzinfo=timezone.utc),
                            dictionary=dictionary)
    return {(r["original_tag"], r["valid_date"], r["frequency"], r["known_at"]): r for r in res.rows}


def _changed(change):
    root = resources.files("company_corpus.concepts")
    concepts = json.loads(root.joinpath("concepts.json").read_text(encoding="utf-8"))
    table = json.loads(root.joinpath("tags", "us_gaap.json").read_text(encoding="utf-8"))
    change(table["concepts"])
    return parse_dictionary(concepts, [("us_gaap", table)])


def test_changing_the_dictionary_changes_no_published_row():
    base = _rows(None)
    assert len(base) > 100

    def reorder(concepts):
        for c in concepts.values():
            c["tags"].reverse()

    def move(concepts):
        tag = "LongTermDebtAndCapitalLeaseObligations"
        concepts["long_term_debt"]["tags"].remove(tag)
        concepts["debt_total"]["tags"].append(tag)

    def add(concepts):
        concepts["goodwill"]["tags"].append("OtherLiabilitiesNoncurrent")

    assert _rows(_changed(reorder)) == base
    assert _rows(_changed(move)) == base
    assert base.items() <= _rows(_changed(add)).items()


def test_the_ifrs_table_changes_no_us_row():
    from company_corpus.concepts.dictionary import load_dictionary

    assert _rows(load_dictionary(("us_gaap", "ifrs_full"))) == _rows(load_dictionary(("us_gaap",)))


def test_form_taxonomy_matches_what_was_published():
    # The whole index, by equality: an added or a removed form changes published rows too.
    got = {form: ft.value[0] for form, ft in taxonomy._EDGAR_FORM_INDEX.items()}
    assert got == GOLDEN["forms"], WHY


def test_forms_outside_the_taxonomy_stay_outside():
    for form in GOLDEN["unmapped_forms"]:
        assert from_edgar_form(form) is None, WHY


def test_currency_anchor_tags_match_what_was_published():
    assert list(DEFAULT_CURRENCY_ANCHOR_TAGS) == GOLDEN["currency_anchor_tags"], WHY


def test_currency_pattern_matches_what_was_published():
    assert financials._CURRENCY_RE.pattern == GOLDEN["currency_pattern"], WHY


def test_source_doc_id_recipe_matches_what_was_published():
    fixed = GOLDEN["source_doc_id"]
    record = FilingRecord(cik=fixed["cik"], form_type=from_edgar_form(fixed["form"]),
                          sec_form=fixed["form"], accession=fixed["accession"])
    assert record.doc_id == fixed["doc_id"], WHY
