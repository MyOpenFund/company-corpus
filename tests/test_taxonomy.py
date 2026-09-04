from __future__ import annotations

import pytest

from company_corpus.taxonomy import (
    FULL_SCOPE,
    FormType,
    by_code,
    from_edgar_form,
    parse_scope,
)


def test_full_scope_is_narrative_families_only():
    families = {ft.family for ft in FULL_SCOPE}
    assert families == {"A", "B", "C", "D"}
    # Ownership (E) and structured financials (F) are opt-in, not in default scope.
    assert FormType.E1 not in FULL_SCOPE
    assert FormType.F1 not in FULL_SCOPE


def test_by_code_roundtrip():
    assert by_code("a1") is FormType.A1
    assert by_code("B1") is FormType.B1
    with pytest.raises(ValueError):
        by_code("ZZ")


@pytest.mark.parametrize(
    "edgar_form, expected",
    [
        ("10-K", FormType.A1),
        ("10-Q", FormType.A2),
        ("20-F", FormType.A3),
        ("8-K", FormType.B1),
        ("6-K", FormType.B2),
        ("DEF 14A", FormType.C1),
        ("S-1", FormType.D1),
        ("424B5", FormType.D3),
        ("4", FormType.E1),
        ("13F-HR", FormType.E2),
        (" sc 13d ", FormType.E3),
    ],
)
def test_from_edgar_form(edgar_form, expected):
    assert from_edgar_form(edgar_form) is expected


def test_from_edgar_form_unknown_returns_none():
    assert from_edgar_form("NT 10-K") is None


def test_parse_scope_variants():
    assert parse_scope(None) == FULL_SCOPE
    assert parse_scope("all") == tuple(FormType)
    assert parse_scope("A") == (FormType.A1, FormType.A2, FormType.A3, FormType.A4)
    assert parse_scope("A1,B1,A1") == (FormType.A1, FormType.B1)  # de-duplicated, ordered
