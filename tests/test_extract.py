from __future__ import annotations

from company_corpus.extract import clean_text, html_to_text, looks_like_html, normalize_whitespace


def test_normalize_whitespace_collapses_runs():
    assert normalize_whitespace("a    b\t c") == "a b c"
    assert normalize_whitespace("x\n\n\n\n\ny") == "x\n\ny"
    assert normalize_whitespace("  trim  \n  me ") == "trim\nme"


def test_html_to_text_strips_markup_and_scripts():
    html = "<html><head><style>.x{}</style></head><body><h1>Title</h1>" \
           "<p>Hello world</p><script>bad()</script></body></html>"
    text = html_to_text(html)
    assert "Title" in text and "Hello world" in text
    assert "bad()" not in text
    assert "<" not in text


def test_looks_like_html_detection():
    assert looks_like_html("", "doc.htm")
    assert looks_like_html("<!DOCTYPE html><html>", "doc")
    assert looks_like_html("<ix:header/>", "doc")  # inline XBRL
    assert not looks_like_html("plain text here", "doc.txt")


def test_clean_text_routes_by_type():
    assert clean_text("<html><body>Hi   there</body></html>", "a.htm") == "Hi there"
    assert clean_text("plain   text", "a.txt") == "plain text"
