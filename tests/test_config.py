from __future__ import annotations

import pytest

from company_corpus.config import (
    DEFAULT_DURATION_BANDS,
    SEC_MAX_REQUESTS_PER_SECOND,
    Config,
    cusip6,
    cusip_full,
    normalize_cik,
    normalize_cusip,
)


def test_user_agent_carries_contact():
    cfg = Config(contact="someone@example.com")
    assert "someone@example.com" in cfg.user_agent
    assert cfg.user_agent.startswith("company-corpus/")


def test_no_contact_means_no_email_in_user_agent(monkeypatch):
    # With the env var unset there is no default contact, and the User-Agent
    # must not embed any email address.
    monkeypatch.delenv("COMPANY_CORPUS_CONTACT", raising=False)
    cfg = Config()
    assert cfg.contact == ""
    assert "@" not in cfg.user_agent
    assert cfg.user_agent == "company-corpus/0.1"


def test_rps_above_sec_limit_is_rejected():
    with pytest.raises(ValueError):
        Config(requests_per_second=SEC_MAX_REQUESTS_PER_SECOND + 1)


def test_min_delay_matches_rps():
    cfg = Config(requests_per_second=10)
    assert cfg.min_delay_seconds == pytest.approx(0.1)


def test_manifest_file_path_is_zero_padded():
    cfg = Config()
    assert cfg.manifest_file("320193").name == "0000320193.jsonl"


def test_reference_paths():
    from pathlib import Path
    cfg = Config(data_dir=Path("/tmp/x/data"))
    assert cfg.reference_dir == Path("/tmp/x/data/reference")
    assert cfg.cik_lookup_path == Path("/tmp/x/data/reference/cik-lookup-data.txt")
    assert cfg.name_cache_path == Path("/tmp/x/data/reference/name_cik_cache.csv")


@pytest.mark.parametrize(
    "raw, expected",
    [
        (320193, "0000320193"),
        ("320193", "0000320193"),
        ("0000320193", "0000320193"),
        ("CIK0000320193", "0000320193"),
    ],
)
def test_normalize_cik(raw, expected):
    assert normalize_cik(raw) == expected


def test_normalize_cik_rejects_empty():
    with pytest.raises(ValueError):
        normalize_cik("abc")


@pytest.mark.parametrize("raw, expected", [
    ("00037BAC6", "00037BAC6"),
    (" 00037bac6 ", "00037BAC6"),
    ("00037BAC", "00037BAC"),
])
def test_normalize_cusip(raw, expected):
    assert normalize_cusip(raw) == expected


@pytest.mark.parametrize("bad", ["", "123", "00037BAC63", "00037B@C6"])
def test_normalize_cusip_rejects_bad_length_or_chars(bad):
    with pytest.raises(ValueError):
        normalize_cusip(bad)


@pytest.mark.parametrize("raw, expected", [
    ("00037BAC6", "00037B"),
    ("00037bac6", "00037B"),
    ("US00037BAC63", "00037B"),
])
def test_cusip6(raw, expected):
    assert cusip6(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("037833AT7", "037833AT7"),     # 9-char CUSIP unchanged
    ("00037BAC", "00037BAC"),       # 8-char CUSIP unchanged
    ("US00037BAC63", "00037BAC6"),  # US ISIN -> embedded CUSIP9
    ("nope", ""),                    # not a CUSIP/ISIN
    ("", ""),
])
def test_cusip_full(raw, expected):
    assert cusip_full(raw) == expected


def test_store_defaults_are_the_fiscal_week_bands():
    c = Config()
    assert c.store_duration_bands == DEFAULT_DURATION_BANDS == (
        ("quarterly", 81, 115),
        ("semiannual", 165, 199),
        ("ytd9", 245, 285),
        ("annual", 360, 374),
    )
    assert c.store_known_at_fallback_tz == "America/New_York"
    assert c.store_known_at_fallback_time == "23:59:59"
    assert c.store_edgar_day_tz == "America/New_York"
    assert c.store_stale_filed_max_days_ahead == 7
    assert c.edgar_country_codes["L2"] == "IE" and c.edgar_country_codes["A6"] == "CA"


def test_country_table_is_per_instance():
    a, b = Config(), Config()
    a.edgar_country_codes["ZZ"] = "XX"
    assert "ZZ" not in b.edgar_country_codes


@pytest.mark.parametrize(
    "bands, match",
    [
        ((("quarterly", 81, 115), ("semiannual", 110, 199)), "overlap"),
        ((("quarterly", 115, 81),), "min <= max"),
        ((("quarterly", 0, 10),), "min <= max"),
    ],
)
def test_bad_duration_bands_are_refused(bands, match):
    with pytest.raises(ValueError, match=match):
        Config(store_duration_bands=bands)


def test_bad_fallback_settings_are_refused():
    with pytest.raises(ValueError, match="time zone"):
        Config(store_known_at_fallback_tz="Mars/Olympus")
    with pytest.raises(ValueError, match="store_edgar_day_tz"):
        Config(store_edgar_day_tz="Mars/Olympus")
    with pytest.raises(ValueError, match="HH:MM:SS"):
        Config(store_known_at_fallback_time="midnight")


def test_crown_dependencies_are_in_the_country_table():
    # Seen on the real universe (country_unknown:Y7); codes per SEC "EDGAR State and Country
    # Codes" (last reviewed 2024-06-05).
    c = Config()
    assert c.edgar_country_codes["Y7"] == "GG"  # Guernsey
    assert c.edgar_country_codes["Y8"] == "IM"  # Isle of Man
    assert c.edgar_country_codes["Y9"] == "JE"  # Jersey


@pytest.mark.parametrize("tags", [(), ("Assets", ""), ("Assets", 3), "Assets", ["Assets"], None])
def test_bad_currency_anchor_tags_are_refused(tags):
    with pytest.raises(ValueError, match="store_currency_anchor_tags"):
        Config(store_currency_anchor_tags=tags)


def test_a_negative_stale_filed_margin_is_refused():
    with pytest.raises(ValueError, match="store_stale_filed_max_days_ahead"):
        Config(store_stale_filed_max_days_ahead=-1)
    assert Config(store_stale_filed_max_days_ahead=0).store_stale_filed_max_days_ahead == 0
