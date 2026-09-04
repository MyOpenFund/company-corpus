"""The AMF (France) backend tags ESEF report-package .zip files so Tier B (Arelle)
can find + parse them; everything else (PDF, bare .xhtml) stays a plain document."""
from company_corpus.eu.sources.oam_fr import _file_kind


def test_zip_is_flagged_esef():
    assert _file_kind("https://ftp.../tadila/INFOFI/FC123_2024.zip") == "esef"
    assert _file_kind("https://ftp.../report.ZIP?download=1") == "esef"


def test_pdf_and_bare_xhtml_stay_document():
    # PDF is not machine-readable; a bare .xhtml lacks the bundled extension taxonomy
    # (Arelle resolves no facts from it standalone) -> not flagged esef.
    assert _file_kind("https://ftp.../FC123_2024.pdf") == "document"
    assert _file_kind("https://ftp.../solutions30-2024.xhtml") == "document"


def test_missing_or_empty_url_is_document():
    assert _file_kind("") == "document"
    assert _file_kind(None) == "document"
