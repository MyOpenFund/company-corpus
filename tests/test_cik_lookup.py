from __future__ import annotations


from company_corpus.sources.cik_lookup import (
    CIK_LOOKUP_URL,
    fetch_cik_lookup,
    parse_cik_lookup,
)

SAMPLE = (
    "APPLE INC:0000320193:\n"
    "APPLE COMPUTER INC:0000320193:\n"   # former name -> same CIK, different key
    "META PLATFORMS INC:0001326801:\n"
    "FACEBOOK INC:0001326801:\n"
    "SUNRISE CORP:0000111111:\n"
    "SUNRISE CORPORATION:0000222222:\n"  # same canonical key -> two CIKs (collision)
    "GARBAGE LINE WITHOUT COLON\n"        # skipped
    ":0000000001:\n"                       # empty name -> skipped
)


def test_parse_groups_former_names_and_collisions():
    index = parse_cik_lookup(SAMPLE)
    assert index["APPLE"] == {"0000320193"}
    assert index["APPLE COMPUTER"] == {"0000320193"}
    assert index["META PLATFORMS"] == {"0001326801"}
    assert index["FACEBOOK"] == {"0001326801"}
    assert index["SUNRISE"] == {"0000111111", "0000222222"}  # collision
    assert "GARBAGE LINE WITHOUT COLON" not in index


def test_fetch_uses_cache_on_second_call(make_fetcher, tmp_path):
    cache = tmp_path / "ref" / "cik-lookup-data.txt"
    fetcher = make_fetcher({"cik-lookup-data.txt": SAMPLE})
    first = fetch_cik_lookup(fetcher, cache)
    assert first == SAMPLE
    assert cache.exists()
    assert len(fetcher.calls) == 1
    second = fetch_cik_lookup(fetcher, cache)   # served from disk, no new GET
    assert second == SAMPLE
    assert len(fetcher.calls) == 1
    assert CIK_LOOKUP_URL.startswith("https://www.sec.gov/")
