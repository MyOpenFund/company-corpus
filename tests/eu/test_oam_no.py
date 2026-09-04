"""Tests for the Norway OAM backend (NewsWebNO — Oslo Børs NewsWeb JSON API).

All network-free: a stub Fetcher routes get_json (list) and post_json (issuers +
message) from the captured fixtures and synthetic data.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import company_corpus.eu.sources.oam_no as _oam_no_module
from company_corpus.eu.documents import DOC_TYPES
from company_corpus.eu.entities import Entity
from company_corpus.eu.sources.oam_no import NewsWebNO, _doc_type, _normalise

FIX = Path(__file__).parent.parent / "fixtures" / "eu"

EQNR_ENTITY = Entity(lei="L1", name="Equinor ASA", country="NO")

# ---------------------------------------------------------------------------
# Load fixtures
# ---------------------------------------------------------------------------

_ISSUERS_FIX = json.loads((FIX / "no_issuers.json").read_text())
_LIST_FIX = json.loads((FIX / "no_list_eqnr.json").read_text())
_MESSAGE_FIX = json.loads((FIX / "no_message.json").read_text())

# The message id used in the message fixture
_MESSAGE_ID = _MESSAGE_FIX["data"]["message"]["messageId"]

# Empty list response (terminates pagination)
_EMPTY_LIST = {"data": {"messages": [], "overflow": False}}


# ---------------------------------------------------------------------------
# Stub fetcher
# ---------------------------------------------------------------------------

class _StubFetcher:
    """Routes calls by URL substring.

    * ``post_json`` on ``/issuers`` → issuers fixture.
    * ``post_json`` on ``/message`` → message fixture (any messageId).
    * ``get_json`` on ``/list`` with ``issuer=EQNR`` → list fixture on first call,
      empty list on subsequent calls (so pagination terminates).
    """

    def __init__(
        self,
        *,
        issuers=None,
        list_resp=None,
        message=None,
    ):
        self._issuers = issuers if issuers is not None else _ISSUERS_FIX
        self._list_resp = list_resp if list_resp is not None else _LIST_FIX
        self._message = message if message is not None else _MESSAGE_FIX
        self._list_calls = 0
        # Faithful-stub bookkeeping: a backend that resolves the wrong
        # issuerSign, or requests an unexpected endpoint, must be observable.
        self.get_urls: list[str] = []
        self.post_urls: list[str] = []
        self.list_issuers_seen: set[str] = set()

    def get_json(self, url: str, **_):
        self.get_urls.append(url)
        if "/list" in url:
            # Only serve filings for the issuerSign the fixture describes; a
            # wrong sign yields nothing, so a mis-resolve fails the test.
            issuer = parse_qs(urlsplit(url).query).get("issuer", [""])[0]
            self.list_issuers_seen.add(issuer)
            if issuer != "EQNR":
                return _EMPTY_LIST
            self._list_calls += 1
            if self._list_calls == 1:
                return self._list_resp
            return _EMPTY_LIST
        raise RuntimeError(f"Unexpected get_json url: {url}")

    def post_json(self, url: str, body: dict, **_):
        self.post_urls.append(url)
        if "/issuers" in url:
            return self._issuers
        if "/message" in url:
            # The attachment lookup must target a concrete messageId.
            assert "messageId=" in url, f"/message call missing messageId: {url}"
            return self._message
        raise RuntimeError(f"Unexpected post_json url: {url}")


def _make_stub(**kwargs) -> _StubFetcher:
    return _StubFetcher(**kwargs)


# ---------------------------------------------------------------------------
# test_resolve_name_to_issuersign_and_discover
# ---------------------------------------------------------------------------

def test_resolve_name_to_issuersign_and_discover():
    """Entity with name 'Equinor ASA' resolves to EQNR and returns ≥1 Document."""
    stub = _make_stub()
    src = NewsWebNO(fetcher=stub)

    docs = src.discover(EQNR_ENTITY)

    assert docs, "expected at least one Document from fixture"
    assert all(d.doc_type in DOC_TYPES for d in docs), "every doc_type must be in DOC_TYPES"
    assert all(d.source == "oam-no" for d in docs), "source must be oam-no"
    assert all(d.lei == "L1" for d in docs), "lei must be passed through"
    assert all(d.country == "NO" for d in docs), "country must be NO"
    # Every file URL must use the correct attachment endpoint
    for d in docs:
        for f in d.files:
            assert f["url"].startswith(
                "https://api3.oslo.oslobors.no/v1/newsreader/attachment?"
            ), f"unexpected URL: {f['url']}"
    # Messages with 0 attachments in the fixture must be skipped
    # (3 out of 8 fixture messages have numbAttachments=0 and the rest ≥1)
    assert len(docs) >= 1
    # Faithful-stub check: the backend resolved 'Equinor ASA' -> EQNR and drove
    # /list with exactly that issuerSign (a mis-resolve would have hit the stub
    # with a different sign and returned no filings).
    assert stub.list_issuers_seen == {"EQNR"}, (
        f"/list must be called with issuer=EQNR, saw {stub.list_issuers_seen}"
    )


def test_discover_skips_zero_attachment_messages():
    """Messages with numbAttachments=0 must not produce a Document."""
    stub = _make_stub()
    src = NewsWebNO(fetcher=stub)
    docs = src.discover(EQNR_ENTITY)

    # The fixture has messages with numbAttachments = 0 (cat 1005, 1008, 1101, 1010)
    # Only the ones with numbAttachments > 0 should produce Documents
    zero_att_msg_ids = {
        m["messageId"]
        for m in _LIST_FIX["data"]["messages"]
        if m.get("numbAttachments", 0) == 0
    }
    doc_ids = {d.doc_id for d in docs}
    for mid in zero_att_msg_ids:
        assert f"no-{mid}" not in doc_ids, (
            f"message {mid} has 0 attachments but produced a Document"
        )


# ---------------------------------------------------------------------------
# test_doc_type_mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cat_ids,expected", [
    ([1001], "annual_report"),
    ([1002], "half_year_report"),
    ([1003], "interim_statement"),
    ([1004], "interim_statement"),
    ([1005], "inside_information"),
    ([1006], "holding_notification"),
    ([1007], "other"),
    ([1010], "other"),
    ([1102], "holding_notification"),
    ([1001, 1005], "annual_report"),   # most specific wins
    ([1005, 1001], "annual_report"),   # order in input doesn't matter
    ([9999], "other"),                 # unknown category
    ([], "other"),                     # no category
])
def test_doc_type_mapping(cat_ids, expected):
    result = _doc_type(cat_ids)
    assert result == expected, f"_doc_type({cat_ids!r}) expected {expected!r}, got {result!r}"


# ---------------------------------------------------------------------------
# test_name_no_match_returns_empty
# ---------------------------------------------------------------------------

def test_name_no_match_returns_empty():
    """An entity name with no match in the issuers list returns [] + records error."""
    stub = _make_stub()
    src = NewsWebNO(fetcher=stub)

    docs = src.discover(Entity(lei=None, name="Acme Fjord Corp AS", country="NO"))

    assert docs == [], "unmatched name must return empty list"
    assert any(e["context"] == "resolve" for e in src.errors), (
        "a resolve error must be recorded for unmatched name"
    )


def test_ambiguous_name_returns_empty():
    """Multiple active matches for a name returns [] + records error (no-guess)."""
    # Inject two issuers with the same normalised name
    dup_issuers = {
        "header": {},
        "data": {
            "issuers": [
                {"issuerId": 1, "issuerSign": "AAA", "name": "Alpha Corp ASA", "isActive": 1},
                {"issuerId": 2, "issuerSign": "BBB", "name": "Alpha Corp ASA", "isActive": 1},
            ]
        },
    }
    stub = _make_stub(issuers=dup_issuers)
    src = NewsWebNO(fetcher=stub)

    docs = src.discover(Entity(lei=None, name="Alpha Corp ASA", country="NO"))

    assert docs == [], "ambiguous name must return empty list"
    assert any(e["context"] == "resolve" for e in src.errors)


def test_inactive_issuer_not_matched():
    """An inactive issuer with the same name must not match."""
    inactive_only = {
        "header": {},
        "data": {
            "issuers": [
                {"issuerId": 1, "issuerSign": "OLD", "name": "Equinor ASA", "isActive": 0},
            ]
        },
    }
    stub = _make_stub(issuers=inactive_only)
    src = NewsWebNO(fetcher=stub)

    docs = src.discover(EQNR_ENTITY)
    assert docs == []
    assert any(e["context"] == "resolve" for e in src.errors)


# ---------------------------------------------------------------------------
# test_pagination_overflow_then_stops
# ---------------------------------------------------------------------------

def test_pagination_overflow_then_stops():
    """Pagination terminates after overflow=false; messages deduped by messageId."""

    # First window: 2 messages, overflow=True
    window1 = {
        "data": {
            "messages": [
                {
                    "messageId": 101,
                    "title": "Msg A",
                    "category": [{"id": 1001}],
                    "issuerSign": "EQNR",
                    "issuerName": "Equinor ASA",
                    "publishedTime": "2023-06-15T10:00:00.000Z",
                    "numbAttachments": 1,
                    "oamMandatory": 1,
                },
                {
                    "messageId": 102,
                    "title": "Msg B",
                    "category": [{"id": 1002}],
                    "issuerSign": "EQNR",
                    "issuerName": "Equinor ASA",
                    "publishedTime": "2023-05-01T10:00:00.000Z",
                    "numbAttachments": 1,
                    "oamMandatory": 0,
                },
            ],
            "overflow": True,
        }
    }
    # Second window: same message 102 (should dedup) + new message 103, overflow=False
    window2 = {
        "data": {
            "messages": [
                {
                    "messageId": 102,
                    "title": "Msg B (duplicate)",
                    "category": [{"id": 1002}],
                    "issuerSign": "EQNR",
                    "issuerName": "Equinor ASA",
                    "publishedTime": "2023-05-01T10:00:00.000Z",
                    "numbAttachments": 1,
                    "oamMandatory": 0,
                },
                {
                    "messageId": 103,
                    "title": "Msg C",
                    "category": [{"id": 1001}],
                    "issuerSign": "EQNR",
                    "issuerName": "Equinor ASA",
                    "publishedTime": "2023-01-10T10:00:00.000Z",
                    "numbAttachments": 1,
                    "oamMandatory": 1,
                },
            ],
            "overflow": False,
        }
    }

    # Message detail stub: same attachment for all
    message_stub = {
        "data": {
            "message": {
                "messageId": 999,
                "issuerSign": "EQNR",
                "issuerName": "Equinor ASA",
                "attachments": [{"id": 1, "name": "doc.pdf"}],
            }
        }
    }

    class _PaginationStub:
        def __init__(self):
            self._get_calls = 0

        def get_json(self, url, **_):
            self._get_calls += 1
            if self._get_calls == 1:
                return window1
            if self._get_calls == 2:
                return window2
            return _EMPTY_LIST  # should not be reached

        def post_json(self, url, body, **_):
            if "/issuers" in url:
                return _ISSUERS_FIX
            if "/message" in url:
                return message_stub
            raise RuntimeError(f"Unexpected post_json: {url}")

    # Monkeypatch _today so the window end is deterministic
    original_today = _oam_no_module._today
    _oam_no_module._today = lambda: "2026-01-01"
    try:
        src = NewsWebNO(fetcher=_PaginationStub())
        docs = src.discover(EQNR_ENTITY)
    finally:
        _oam_no_module._today = original_today

    # 3 unique messageIds (101, 102, 103) but 102 is deduplicated → 3 Documents
    doc_ids = [d.doc_id for d in docs]
    assert len(doc_ids) == 3, f"expected 3 docs (deduplicated), got {len(doc_ids)}: {doc_ids}"
    assert "no-101" in doc_ids
    assert "no-102" in doc_ids
    assert "no-103" in doc_ids
    assert not src.errors, f"unexpected errors: {src.errors}"


def test_pagination_cap_records_truncated():
    """Reaching _MAX_WINDOWS records a truncated error."""

    overflow_page = {
        "data": {
            "messages": [
                {
                    "messageId": 1000,
                    "title": "X",
                    "category": [{"id": 1007}],
                    "issuerSign": "EQNR",
                    "issuerName": "Equinor ASA",
                    "publishedTime": "2020-01-01T00:00:00.000Z",
                    "numbAttachments": 0,
                    "oamMandatory": 0,
                }
            ],
            "overflow": True,
        }
    }
    message_stub = {"data": {"message": {"messageId": 1000, "attachments": []}}}

    class _InfiniteOverflowStub:
        def get_json(self, url, **_):
            return overflow_page

        def post_json(self, url, body, **_):
            if "/issuers" in url:
                return _ISSUERS_FIX
            return message_stub

    original_today = _oam_no_module._today
    _oam_no_module._today = lambda: "2026-01-01"
    try:
        src = NewsWebNO(fetcher=_InfiniteOverflowStub())
        src.discover(EQNR_ENTITY)
    finally:
        _oam_no_module._today = original_today

    assert any(e["context"] == "truncated" for e in src.errors), (
        "expected a truncated error when _MAX_WINDOWS exceeded"
    )


# ---------------------------------------------------------------------------
# Robustness: network failures do not abort the whole run
# ---------------------------------------------------------------------------

def test_list_fetch_failure_recorded_returns_empty():
    """A failing /list GET records an error and returns []."""

    class _ListFailStub:
        def get_json(self, url, **_):
            raise ConnectionError("list down")

        def post_json(self, url, body, **_):
            return _ISSUERS_FIX

    src = NewsWebNO(fetcher=_ListFailStub())
    docs = src.discover(EQNR_ENTITY)

    assert docs == []
    assert any(e["context"] == "list" for e in src.errors)


def test_message_fetch_failure_skips_message():
    """A failing /message POST records an error but continues with other messages."""

    class _MessageFailStub:
        def __init__(self):
            self._list_calls = 0

        def get_json(self, url, **_):
            self._list_calls += 1
            if self._list_calls == 1:
                return _LIST_FIX
            return _EMPTY_LIST

        def post_json(self, url, body, **_):
            if "/issuers" in url:
                return _ISSUERS_FIX
            if "/message" in url:
                raise ConnectionError("message down")
            raise RuntimeError(f"Unexpected: {url}")

    src = NewsWebNO(fetcher=_MessageFailStub())
    docs = src.discover(EQNR_ENTITY)

    # Failures are recorded
    assert any(e["context"] == "message" for e in src.errors)
    # But the run did not raise; docs may be empty since all message hops failed
    assert isinstance(docs, list)


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def test_list_issuers_returns_empty():
    stub = _make_stub()
    src = NewsWebNO(fetcher=stub)
    assert src.list_issuers() == []


def test_normalise():
    assert _normalise("Equinor ASA") == "equinor"
    assert _normalise("  Yara International ASA  ") == "yara international"
    assert _normalise("DNB Bank ASA.") == "dnb bank"  # period removed → " asa" stripped
    assert _normalise("DNB Bank ASA") == "dnb bank"   # " asa" → stripped
    assert _normalise("Orkla ASA") == "orkla"
    assert _normalise("Mowi ASA") == "mowi"
    assert _normalise("Some Corp AS") == "some corp"  # " as" → stripped


def test_normalise_foreign_legal_forms():
    """Oslo's foreign tail (shipping/offshore) uses Limited/Ltd/plc/Inc — and
    GLEIF often spells the form differently, so both must collapse to one core."""
    # GLEIF spelling vs Oslo spelling -> same core
    assert _normalise("Golden Ocean Group Limited") == _normalise("Golden Ocean Group Ltd")
    assert _normalise("2020 Bulkers Ltd.") == _normalise("2020 BULKERS LTD.") == "2020 bulkers"
    assert _normalise("Frontline plc") == _normalise("FRONTLINE PLC") == "frontline"
    assert _normalise("Borr Drilling Limited") == "borr drilling"
    assert _normalise("Stolt-Nielsen Limited") == "stolt-nielsen"
    # distinctive words are kept (no over-stripping) -> still distinguishes issuers
    assert _normalise("Golden Ocean Group Ltd") != _normalise("Golden Energy Group Ltd")
    # German/Swedish forms that actually occur in the live list are handled too
    assert _normalise("Mutares SE & Co. KGaA") != ""  # not over-stripped to empty
    assert _normalise("Siemens AG") == "siemens" and _normalise("Volvo AB") == "volvo"


def test_normalise_empty_and_collapse_safety():
    """Empty/whitespace -> empty (and the resolver's exactly-one-match rule means
    a same-core collision declines rather than mis-binds — never a guessed bind)."""
    assert _normalise("") == ""
    assert _normalise("   ") == ""
    assert _normalise(None) == ""  # None-guarded
    # An AS and an ASA of the same name collapse to one core *by design*; the
    # no-guess resolver then sees >1 active match and declines (tested elsewhere),
    # so this is a conservative miss, not a wrong bind.
    assert _normalise("Atea AS") == _normalise("Atea ASA") == "atea"


def test_esef_kind():
    """Files with .zip or .xhtml extension must get kind='esef'."""
    stub = _make_stub()
    src = NewsWebNO(fetcher=stub)
    docs = src.discover(EQNR_ENTITY)
    # The annual report message (614113) has an eqnr20231231NO.zip attachment
    ar_docs = [d for d in docs if d.doc_id == f"no-{_MESSAGE_ID}"]
    if ar_docs:
        zip_files = [f for f in ar_docs[0].files if f["name"].endswith(".zip")]
        assert all(f["kind"] == "esef" for f in zip_files), (
            ".zip attachments must have kind='esef'"
        )
