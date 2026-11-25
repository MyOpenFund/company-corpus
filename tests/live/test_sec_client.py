"""SecClient: explicit contact, error mapping, retries, spacing shared across threads."""
from __future__ import annotations

import threading
import time as real_time

import pytest
import requests

from company_corpus import http as http_mod
from company_corpus.live.sec import (
    SecClient, SecContactMissing, SecNotFound, SecRefused, SecUnavailable,
)
from tests.test_http import FakeResponse, FakeSession

_REAL_SLEEP = real_time.sleep  # http_mod.time is the time module: patched below
CONTACT = "a@example.org"
URL = "https://data.sec.gov/submissions/CIK0000000001.json"


class _Resp(FakeResponse):
    def __init__(self, status_code=200, *, body=b"", headers=None):
        super().__init__(status_code, headers=headers)
        self._body = body

    @property
    def content(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8")


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: None)


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_contact_raises(blank):
    with pytest.raises(SecContactMissing):
        SecClient(blank)


def test_user_agent_carries_contact():
    session = FakeSession([_Resp(200, body=b"{}")])
    SecClient(CONTACT, session=session)
    assert session.headers["User-Agent"] == f"company-corpus/0.1 ({CONTACT})"


def test_get_bytes_returns_body_and_ignores_max_age():
    session = FakeSession([_Resp(200, body=b"abc")])
    assert SecClient(CONTACT, session=session).get_bytes(URL, max_age_s=60.0) == b"abc"


def test_403_undeclared_page_raises_refused():
    body = b"<html>Your Request Originates from an Undeclared Automated Tool</html>"
    session = FakeSession([_Resp(403, body=body)])
    with pytest.raises(SecRefused):
        SecClient(CONTACT, session=session).get_bytes(URL, max_age_s=None)


def test_403_other_raises_http_error():
    session = FakeSession([_Resp(403, body=b"forbidden")])
    with pytest.raises(requests.HTTPError) as ei:
        SecClient(CONTACT, session=session).get_bytes(URL, max_age_s=None)
    assert not isinstance(ei.value, SecRefused)


def test_404_raises_not_found():
    session = FakeSession([_Resp(404, body=b"nope")])
    with pytest.raises(SecNotFound):
        SecClient(CONTACT, session=session).get_bytes(URL, max_age_s=None)
    assert len(session.calls) == 1


def test_429_then_200_returns_body():
    session = FakeSession([_Resp(429, headers={"Retry-After": "0"}), _Resp(200, body=b"ok")])
    assert SecClient(CONTACT, session=session).get_bytes(URL, max_age_s=None) == b"ok"
    assert len(session.calls) == 2


def test_exhausted_retries_raise_unavailable():
    session = FakeSession([_Resp(503)])
    with pytest.raises(SecUnavailable):
        SecClient(CONTACT, retries=4, session=session).get_bytes(URL, max_age_s=None)
    assert len(session.calls) == 5


def test_connection_errors_exhausted_raise_unavailable():
    session = FakeSession([requests.ConnectionError("boom")])
    with pytest.raises(SecUnavailable):
        SecClient(CONTACT, retries=1, session=session).get_bytes(URL, max_age_s=None)
    assert len(session.calls) == 2


# max_rps is capped at the SEC limit (10/s) by Config, so 10 rps -> 0.1 s spacing.
def test_spacing_shared_across_threads(monkeypatch):
    now = [0.0]
    clock_lock = threading.Lock()

    def monotonic():
        with clock_lock:
            return now[0]

    def sleep(s):
        with clock_lock:
            deadline = now[0] + s
        _REAL_SLEEP(0.002)  # yield so unguarded threads interleave
        with clock_lock:  # concurrent sleepers wake together, as in real time
            now[0] = max(now[0], deadline)

    monkeypatch.setattr(http_mod.time, "monotonic", monotonic)
    monkeypatch.setattr(http_mod.time, "sleep", sleep)

    starts: list[float] = []
    session = FakeSession([_Resp(200, body=b"x")])
    client = SecClient(CONTACT, max_rps=10.0, session=session)
    orig = client._fetcher._throttle_unlocked

    def recording(url):  # runs inside the lock: start time is deterministic
        orig(url)
        starts.append(monotonic())

    client._fetcher._throttle_unlocked = recording

    def work():
        for _ in range(3):
            client.get_bytes(URL, max_age_s=None)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(starts) == 12
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 0.1 - 1e-9


@pytest.mark.parametrize("kw", [{"max_rps": 0}, {"max_rps": -1.0}, {"retries": -1},
                                {"timeout_s": 0}, {"timeout_s": -5.0}])
def test_invalid_numeric_params_raise(kw):
    with pytest.raises(ValueError):
        SecClient(CONTACT, **kw)


@pytest.mark.parametrize("contact", ["a@example.org\nX-Evil: 1", "a@example.org\r"])
def test_contact_with_line_break_raises(contact):
    with pytest.raises(SecContactMissing):
        SecClient(contact)


def test_rate_cap_shared_across_sec_hosts(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(http_mod.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(http_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    starts: list[float] = []

    class TimedSession(FakeSession):
        def get(self, url, **kw):
            starts.append(now[0])
            return super().get(url, **kw)

    client = SecClient(CONTACT, max_rps=10.0, session=TimedSession([_Resp(200, body=b"x")]))
    for i in range(6):
        host = "data.sec.gov" if i % 2 == 0 else "www.sec.gov"
        client.get_bytes(f"https://{host}/x{i}", max_age_s=None)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 0.1 - 1e-9
