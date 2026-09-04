from __future__ import annotations

from company_corpus.submission import (
    filename_from_url,
    parse_submission,
    select_primary,
)


def test_parse_splits_documents(sample_submission):
    docs = parse_submission(sample_submission)
    assert [d.type for d in docs] == ["10-K", "EX-21.1", "GRAPHIC"]
    primary = docs[0]
    assert primary.sequence == "1"
    assert primary.filename == "aapl-20240928.htm"
    assert primary.description == "10-K"
    assert "Annual Report" in primary.text


def test_select_primary_by_filename(sample_submission):
    docs = parse_submission(sample_submission)
    p = select_primary(docs, primary_filename="aapl-20240928.htm")
    assert p.type == "10-K"


def test_select_primary_by_form_when_no_filename(sample_submission):
    docs = parse_submission(sample_submission)
    p = select_primary(docs, sec_form="10-K")
    assert p.filename == "aapl-20240928.htm"


def test_select_primary_falls_back_to_sequence_one(sample_submission):
    docs = parse_submission(sample_submission)
    p = select_primary(docs, primary_filename="missing.htm", sec_form="ZZ")
    assert p.sequence == "1"


def test_graphic_is_not_text_like(sample_submission):
    docs = parse_submission(sample_submission)
    graphic = next(d for d in docs if d.type == "GRAPHIC")
    assert graphic.is_text_like is False


def test_filename_from_url():
    assert filename_from_url("https://x/y/aapl-20240928.htm") == "aapl-20240928.htm"
    assert filename_from_url("") == ""


def test_parse_empty_submission():
    assert parse_submission("no documents here") == []
    assert select_primary([]) is None
