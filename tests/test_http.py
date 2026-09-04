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
    """Returns canned responses in order (repeating the last); records calls."""

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
