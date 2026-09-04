from __future__ import annotations

from company_corpus.openfigi import (
    OPENFIGI_URL,
    coverage_hint,
    map_identifiers,
)


class _FakePoster:
    """Records calls and replays canned OpenFIGI batch responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append({"url": url, "body": body, "headers": headers})
        return self.responses.pop(0)


def test_map_identifiers_parses_match_and_miss():
    poster = _FakePoster([[
        {"data": [{"name": "ABB FINANCE USA INC", "ticker": "ABBNVX 4.375 05/08/42",
                   "securityType": "GLOBAL", "exchCode": "TRACE",
                   "marketSector": "Corp", "figi": "BBG00ABC1234"}]},
        {"warning": "No identifier found."},
    ]])
    out = map_identifiers(["US00037BAC63", "USNOPE0000000"], post=poster, pause=0)
    assert out["US00037BAC63"].name == "ABB FINANCE USA INC"
    assert out["US00037BAC63"].security_type == "GLOBAL"
    assert out["US00037BAC63"].figi == "BBG00ABC1234"
    assert out["USNOPE0000000"] is None
    assert poster.calls[0]["url"] == OPENFIGI_URL
    assert b"ID_ISIN" in poster.calls[0]["body"]


def test_map_identifiers_batches_when_over_batch_size():
    poster = _FakePoster([
        [{"data": [{"name": "A"}]}, {"data": [{"name": "B"}]}],
        [{"data": [{"name": "C"}]}],
    ])
    out = map_identifiers(["I1", "I2", "I3"], post=poster, batch_size=2, pause=0)
    assert {k: v.name for k, v in out.items()} == {"I1": "A", "I2": "B", "I3": "C"}
    assert len(poster.calls) == 2


def test_map_identifiers_sends_api_key_header_when_given():
    poster = _FakePoster([[{"data": [{"name": "X"}]}]])
    map_identifiers(["I1"], api_key="secret", post=poster, pause=0)
    assert poster.calls[0]["headers"].get("X-OPENFIGI-APIKEY") == "secret"


def test_map_identifiers_accepts_cusip_id_type():
    poster = _FakePoster([[{"data": [{"name": "Y"}]}]])
    map_identifiers(["037833AT7"], id_type="cusip", post=poster, pause=0)
    assert b"ID_CUSIP" in poster.calls[0]["body"]


def test_coverage_hint_jurisdiction_neutral_buckets():
    assert coverage_hint("GLOBAL") == "registry_candidate"
    assert coverage_hint("US DOMESTIC") == "registry_candidate"
    assert coverage_hint("PRIV PLACEMENT") == "private_placement"
    assert coverage_hint("") == "unknown"
    # private markers win over GLOBAL in a compound type
    assert coverage_hint("GLOBAL 144A") == "private_placement"


def test_map_identifiers_handles_short_response():
    # If OpenFIGI returns fewer entries than the batch, every input still gets a
    # key (the missing ones map to None) -- the dict contract holds.
    poster = _FakePoster([[{"data": [{"name": "A"}]}]])  # 1 entry for 2 inputs
    out = map_identifiers(["I1", "I2"], post=poster, batch_size=5, pause=0)
    assert out["I1"].name == "A"
    assert out["I2"] is None
