"""Golden test: the shared derivation tables that published fundamentals rows depend on.

``financials.CONCEPTS`` (which tags feed a concept, in priority order, with its unit and
instant/duration shape) and ``taxonomy.from_edgar_form`` (which form gives which
``source_doc_id`` family) are shared with the F1 engine, but ``store-batches`` publishes rows
derived from them. A fundamentals row's ``known_at`` is the filing's acceptance time, which
never moves, and a refresh of the submissions archive does not change it.

Changing one of these tables therefore alters rows already published at the same
``known_at``: the publisher quarantines the batch as ``conflicting_republish``. A new tag may
only be appended at the lowest priority of its concept; anything else needs a store migration.
The expected values are in ``published_derivations.json`` next to this module.
"""
import json
from pathlib import Path

from company_corpus.financials import CONCEPTS
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
    got = {}
    for form in GOLDEN["forms"]:
        ft = from_edgar_form(form)
        got[form] = ft.value[0] if ft else None
    assert got == GOLDEN["forms"], WHY
