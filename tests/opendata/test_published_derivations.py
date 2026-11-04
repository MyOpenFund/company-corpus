"""Golden test: the shared derivation tables that published fundamentals rows depend on.

``financials.CONCEPTS`` (which tags feed a concept, in priority order, with its unit and
instant/duration shape), the whole form index behind ``taxonomy.from_edgar_form`` (which form
gives which ``source_doc_id`` family), the ``source_doc_id`` recipe, the currency anchor tags
and the ISO currency pattern are shared with the F1 engine or frozen by it, but
``store-batches`` publishes rows derived from them. A fundamentals row's ``known_at`` is the filing's acceptance time, which
never moves, and a refresh of the submissions archive does not change it.

Changing one of these tables therefore alters rows already published at the same
``known_at``: the publisher quarantines the batch as ``conflicting_republish``. A new tag may
only be appended at the lowest priority of its concept; anything else needs a store migration.
The expected values are in ``published_derivations.json`` next to this module.
"""
import json
from pathlib import Path

from company_corpus import financials, taxonomy
from company_corpus.config import DEFAULT_CURRENCY_ANCHOR_TAGS
from company_corpus.financials import CONCEPTS
from company_corpus.models import FilingRecord
from company_corpus.taxonomy import from_edgar_form

GOLDEN = json.loads((Path(__file__).parent / "published_derivations.json").read_text("utf-8"))

WHY = ("Changing this alters rows already published at the same known_at, so the publisher "
       "will quarantine them (conflicting_republish). A new tag may only be appended at "
       "lowest priority; anything else needs a store migration.")


def test_concepts_match_what_was_published():
    got = [{"key": c.key, "tags": list(c.tags), "unit": c.unit, "instant": c.instant}
           for c in CONCEPTS]
    assert got == GOLDEN["concepts"], WHY


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
