"""Unit tests for the SEC fair-access HTTP layer.

These exercise Fetcher directly against a fake requests.Session -- the rest of
the suite mocks Fetcher wholesale, leaving throttling, retry/backoff, status
handling, streaming and the User-Agent header (the SEC-compliance surface)
otherwise untested.
"""
from __future__ import annotations

import pytest
import requests

from company_corpus.config import Config
from company_corpus.http import Fetcher


class FakeResponse:
    def __init__(self, status_code=200, *, text="", json_data=None, chunks=None, headers=None):
        self.status_code = status_code
        self._text = text
        self._json = json_data
        self._chunks = chunks or []
        self.headers = headers or {}
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)

    @property
    def text(self):
        return self._text

    def json(self):
        return self._json

    def iter_content(self, chunk_size=1):
        yield from self._chunks


class FakeSession:
    """Returns canned responses in order (repeating the last); records calls.

    An entry that is an exception instance is *raised* instead of returned
    (models a connection error / timeout on that attempt).
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []
        self._i = 0

    def get(self, url, stream=False, timeout=None, headers=None, params=None):
        self.calls.append({"url": url, "stream": stream, "timeout": timeout,
                           "headers": headers, "params": params})
        resp = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        if isinstance(resp, Exception):
            raise resp
        return resp

    def post(self, url, json=None, data=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "data": data, "timeout": timeout,
                           "headers": headers})
        resp = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        if isinstance(resp, Exception):
            raise resp
        return resp


def _real_response(body: bytes, content_type: str, status: int = 200) -> requests.Response:
    """A genuine requests.Response, encoded the way requests does off the wire:
    the header charset when declared, else requests' RFC 2616 default
    (ISO-8859-1 for text/*) — the source of the mojibake being tested."""
    from requests.structures import CaseInsensitiveDict
    from requests.utils import get_encoding_from_headers

    resp = requests.Response()
    resp.status_code = status
    resp._content = body
    resp.headers = CaseInsensitiveDict({"Content-Type": content_type})
    resp.encoding = get_encoding_from_headers(resp.headers)
    return resp


@pytest.fixture
def cfg():
    return Config(contact="test@example.com")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Collapse retry/throttle backoff so tests don't actually wait.
    monkeypatch.setattr("company_corpus.http.time.sleep", lambda _s: None)


def test_user_agent_carries_contact(cfg):
    sess = FakeSession([FakeResponse(text="ok")])
    Fetcher(cfg, session=sess)
    ua = sess.headers["User-Agent"]
    assert ua.startswith("company-corpus/")
    assert "test@example.com" in ua


def test_retries_on_429_then_succeeds(cfg):
    sess = FakeSession([FakeResponse(429), FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://www.sec.gov/a") == "ok"
    assert len(sess.calls) == 2  # one retry


def test_retries_on_503_then_raises_after_max(cfg):
    cfg = Config(contact="test@example.com", max_retries=2)
    sess = FakeSession([FakeResponse(503)])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.HTTPError):
        f.get("https://www.sec.gov/a")
    assert len(sess.calls) == 3  # initial attempt + 2 retries


def test_default_timeout_passed_through(cfg):
    sess = FakeSession([FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    f.get_text("https://www.sec.gov/a")
    assert sess.calls[0]["timeout"] == cfg.timeout
    assert sess.calls[0]["stream"] is False


def test_get_json_parses(cfg):
    sess = FakeSession([FakeResponse(json_data={"a": 1})])
    f = Fetcher(cfg, session=sess)
    assert f.get_json("https://data.sec.gov/x")["a"] == 1


def test_get_text_and_json_forward_query_params(cfg):
    """params are passed through to session.get (used by the EQS admin-ajax feed)."""
    sess = FakeSession([FakeResponse(text="ok"), FakeResponse(json_data={"ok": 1})])
    f = Fetcher(cfg, session=sess)
    f.get_text("https://www.eqs-news.com/x", params={"filter[search]": "ABB", "pageNo": 1})
    f.get_json("https://www.eqs-news.com/y", params={"companyId": "abc"})
    assert sess.calls[0]["params"] == {"filter[search]": "ABB", "pageNo": 1}
    assert sess.calls[1]["params"] == {"companyId": "abc"}


def test_download_streams_with_download_timeout(cfg, tmp_path):
    sess = FakeSession([FakeResponse(chunks=[b"abc", b"", b"de"])])
    f = Fetcher(cfg, session=sess)
    dest = tmp_path / "nested" / "f.txt"
    written = f.download("https://www.sec.gov/big", dest)
    assert written == 5
    assert dest.read_bytes() == b"abcde"
    assert sess.calls[0]["stream"] is True
    assert sess.calls[0]["timeout"] == cfg.download_timeout  # hard deadline, not the 30s one


def test_tls_verification_on_by_default(cfg):
    sess = FakeSession([FakeResponse(text="ok")])
    Fetcher(cfg, session=sess)
    assert sess.verify is True


def test_tls_verification_can_be_disabled():
    cfg = Config(contact="test@example.com", verify_tls=False)
    sess = FakeSession([FakeResponse(text="ok")])
    Fetcher(cfg, session=sess)
    assert sess.verify is False


def test_retry_after_header_is_honored(monkeypatch):
    cfg = Config(contact="test@example.com", requests_per_second=0)  # no throttle sleep
    sleeps = []
    monkeypatch.setattr("company_corpus.http.time.sleep", lambda s: sleeps.append(s))
    sess = FakeSession([FakeResponse(503, headers={"Retry-After": "7"}),
                        FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://www.sec.gov/a") == "ok"
    assert sleeps == [7.0]  # server's delay used verbatim, not the backoff curve


def test_backoff_is_jittered_within_bounds(monkeypatch):
    cfg = Config(contact="test@example.com", requests_per_second=0)  # no throttle sleep
    sleeps = []
    monkeypatch.setattr("company_corpus.http.time.sleep", lambda s: sleeps.append(s))
    sess = FakeSession([FakeResponse(503), FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    f.get_text("https://www.sec.gov/a")
    assert len(sleeps) == 1
    assert 0.5 <= sleeps[0] <= 1.0  # attempt 0: base 1s, jittered to 50-100%


def test_post_json_returns_parsed_json(cfg):
    """Fetcher.post_json POSTs the JSON body via session.post and returns parsed JSON."""

    class _FakeSessionWithPost:
        """Minimal fake session that records both get and post calls."""

        def __init__(self, response):
            self.headers = {}
            self.verify = True
            self._response = response
            self.post_calls = []

        def get(self, url, **_):  # needed for Fetcher.__init__
            raise RuntimeError("get called unexpectedly")

        def post(self, url, json=None, timeout=None, **_):
            self.post_calls.append({"url": url, "json": json, "timeout": timeout})
            return self._response

    resp = FakeResponse(json_data={"result": "ok"})
    sess = _FakeSessionWithPost(resp)
    f = Fetcher(cfg, session=sess)
    result = f.post_json("https://consob.1info.it/PORTALE1INFO/API/Documenti",
                         {"draw": 1, "start": 0, "length": 200})
    assert result == {"result": "ok"}
    assert len(sess.post_calls) == 1
    assert sess.post_calls[0]["json"] == {"draw": 1, "start": 0, "length": 200}


def test_post_json_retries_on_429(cfg):
    """Fetcher.post_json retries on 429 just like get()."""

    class _RetrySession:
        def __init__(self, responses):
            self.headers = {}
            self.verify = True
            self._responses = list(responses)
            self._i = 0
            self.calls = 0

        def get(self, *a, **kw):
            raise RuntimeError("get called unexpectedly")

        def post(self, url, json=None, timeout=None, **_):
            resp = self._responses[min(self._i, len(self._responses) - 1)]
            self._i += 1
            self.calls += 1
            return resp

    sess = _RetrySession([FakeResponse(429), FakeResponse(json_data={"ok": True})])
    f = Fetcher(cfg, session=sess)
    result = f.post_json("https://consob.1info.it/PORTALE1INFO/API/Documenti", {})
    assert result == {"ok": True}
    assert sess.calls == 2


def test_throttles_repeated_same_host(monkeypatch):
    cfg = Config(contact="test@example.com", requests_per_second=10.0)  # min_delay 0.1s
    sleeps = []
    monkeypatch.setattr("company_corpus.http.time.sleep", lambda s: sleeps.append(s))

    class Clock:
        def __init__(self, vals):
            self.vals, self.i = list(vals), 0

        def __call__(self):
            v = self.vals[min(self.i, len(self.vals) - 1)]
            self.i += 1
            return v

    # call1 sets last=100.0; call2 reads 100.05 (->wait 0.05) then sets 100.2.
    monkeypatch.setattr("company_corpus.http.time.monotonic", Clock([100.0, 100.05, 100.2]))
    sess = FakeSession([FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    f.get_text("https://www.sec.gov/a")
    f.get_text("https://www.sec.gov/a")
    assert sleeps and sleeps[0] == pytest.approx(0.05, abs=1e-6)


def test_post_text_returns_body_and_posts_form_data(cfg):
    """Fetcher.post_text POSTs form data via session.post and returns the text body."""

    class _FormSession:
        def __init__(self, response):
            self.headers = {}
            self.verify = True
            self._response = response
            self.post_calls = []

        def get(self, *a, **kw):
            raise RuntimeError("get called unexpectedly")

        def post(self, url, data=None, timeout=None, **_):
            self.post_calls.append({"url": url, "data": data, "timeout": timeout})
            return self._response

    resp = FakeResponse(text="<html>ok</html>")
    sess = _FormSession(resp)
    f = Fetcher(cfg, session=sess)
    body = f.post_text("https://www.cnmv.es/portal/Consultas/BusquedaPorEntidad",
                       {"ctl00$ContentPrincipal$txtBusqueda": "IBERDROLA"})
    assert body == "<html>ok</html>"
    assert len(sess.post_calls) == 1
    assert sess.post_calls[0]["data"] == {"ctl00$ContentPrincipal$txtBusqueda": "IBERDROLA"}


def test_post_text_retries_on_429(cfg):
    """Fetcher.post_text retries on 429 just like get()/post_json."""

    class _RetryFormSession:
        def __init__(self, responses):
            self.headers = {}
            self.verify = True
            self._responses = list(responses)
            self._i = 0
            self.calls = 0

        def get(self, *a, **kw):
            raise RuntimeError("get called unexpectedly")

        def post(self, url, data=None, timeout=None, **_):
            resp = self._responses[min(self._i, len(self._responses) - 1)]
            self._i += 1
            self.calls += 1
            return resp

    sess = _RetryFormSession([FakeResponse(429), FakeResponse(text="<html>done</html>")])
    f = Fetcher(cfg, session=sess)
    assert f.post_text("https://www.cnmv.es/x", {}) == "<html>done</html>"
    assert sess.calls == 2


def test_get_json_forwards_per_request_headers(cfg):
    """headers= must be merged into the single request, NOT the shared session
    (so one backend's Accept-Language can't contaminate another's requests)."""
    sess = FakeSession([FakeResponse(json_data={"ok": 1})])
    f = Fetcher(cfg, session=sess)
    f.get_json("https://x/y", headers={"Accept-Language": "en"})
    assert sess.calls[0]["headers"] == {"Accept-Language": "en"}
    assert "Accept-Language" not in sess.headers, "must not leak onto the session"


# --- Retry predicate: transient failures only (README "Fair access") ---------


def test_404_is_not_retried_and_costs_one_request(cfg):
    """A 404 is a definitive answer: retrying it burns the host's quota and
    ~4 s of backoff for nothing. Exactly one request, then HTTPError."""
    sess = FakeSession([FakeResponse(404)])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.HTTPError):
        f.get("https://www.sec.gov/missing")
    assert len(sess.calls) == 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_other_4xx_raise_immediately(cfg, status):
    sess = FakeSession([FakeResponse(status)])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.HTTPError):
        f.get("https://www.sec.gov/x")
    assert len(sess.calls) == 1


def test_429_then_200_is_two_requests(cfg):
    sess = FakeSession([FakeResponse(429), FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    assert f.get("https://www.sec.gov/a").status_code == 200
    assert len(sess.calls) == 2


def test_503_is_retried_max_retries_times_then_raises(cfg):
    cfg = Config(contact="test@example.com", max_retries=3)
    sess = FakeSession([FakeResponse(503)])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.HTTPError):
        f.get("https://www.sec.gov/a")
    assert len(sess.calls) == 4  # initial + 3 retries


def test_connection_error_then_200_succeeds(cfg):
    sess = FakeSession([requests.ConnectionError("reset by peer"), FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://www.sec.gov/a") == "ok"
    assert len(sess.calls) == 2


def test_timeout_then_200_succeeds(cfg):
    sess = FakeSession([requests.ReadTimeout("slow"), FakeResponse(text="ok")])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://www.sec.gov/a") == "ok"
    assert len(sess.calls) == 2


def test_connection_error_exhausts_retries_then_raises(cfg):
    cfg = Config(contact="test@example.com", max_retries=2)
    sess = FakeSession([requests.ConnectionError("down")])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.ConnectionError):
        f.get("https://www.sec.gov/a")
    assert len(sess.calls) == 3


def test_non_transient_request_exception_is_not_retried(cfg):
    """A malformed URL / missing schema is a caller bug, not a flaky network."""
    sess = FakeSession([requests.exceptions.MissingSchema("bad url")])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.exceptions.MissingSchema):
        f.get("www.sec.gov/a")
    assert len(sess.calls) == 1


def test_post_json_404_is_one_request(cfg):
    sess = FakeSession([FakeResponse(404)])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.HTTPError):
        f.post_json("https://consob.1info.it/x", {})
    assert len(sess.calls) == 1


def test_post_json_connection_error_then_200(cfg):
    sess = FakeSession([requests.ConnectionError("reset"), FakeResponse(json_data={"ok": 1})])
    f = Fetcher(cfg, session=sess)
    assert f.post_json("https://consob.1info.it/x", {}) == {"ok": 1}
    assert len(sess.calls) == 2


def test_post_text_404_is_one_request(cfg):
    sess = FakeSession([FakeResponse(404)])
    f = Fetcher(cfg, session=sess)
    with pytest.raises(requests.HTTPError):
        f.post_text("https://www.cnmv.es/x", {})
    assert len(sess.calls) == 1


# --- Charset handling: no declared charset -> sniff, declared -> respected ---


def test_get_text_decodes_charset_less_utf8_body(cfg):
    """requests defaults text/* without a charset to ISO-8859-1, so a UTF-8
    page came back as 'cafÃ©'. Sniff the body instead."""
    sess = FakeSession([_real_response(b"caf\xc3\xa9", "text/html")])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://www.amf-france.org/x") == "caf\u00e9"


def test_get_text_respects_declared_charset(cfg):
    """A declared charset wins over sniffing (the sniffer guesses utf_16_be for
    this 4-byte latin-1 body, which would be wrong)."""
    sess = FakeSession([_real_response(b"caf\xe9", "text/html; charset=iso-8859-1")])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://www.cnmv.es/x") == "caf\u00e9"


def test_get_text_declared_utf8_is_respected(cfg):
    sess = FakeSession([_real_response(b"caf\xc3\xa9", "text/html; charset=UTF-8")])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://x/y") == "caf\u00e9"


def test_post_text_decodes_charset_less_utf8_body(cfg):
    sess = FakeSession([_real_response(b"<p>caf\xc3\xa9 \xe2\x82\xac</p>", "text/html")])
    f = Fetcher(cfg, session=sess)
    assert f.post_text("https://www.cnmv.es/x", {}) == "<p>caf\u00e9 \u20ac</p>"


def test_get_text_without_content_type_falls_back_to_utf8(cfg):
    """No Content-Type at all and an undecidable body: never leave encoding
    unset (requests would sniff anyway) — the fallback is UTF-8, not latin-1."""
    resp = _real_response(b"", "text/html")
    resp.headers = {}
    resp.encoding = None
    sess = FakeSession([resp])
    f = Fetcher(cfg, session=sess)
    assert f.get_text("https://x/y") == ""
    assert resp.encoding.lower().replace("_", "-") in ("utf-8", "ascii")
