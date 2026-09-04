"""The suite's network guard actually blocks a real HTTP fetch.

`pytest.ini`'s `--disable-socket` blocks sockets for every test so none can
silently crawl a live SEC/OAM/register endpoint. A guard nobody exercises
rots: these tests build the REAL `company_corpus.http.Fetcher` (its `.session`
untouched -- no fake, no monkeypatch of `requests`) and prove the guard stops
it, so a future refactor that drops the option or the `pytest-socket`
dependency fails here instead of quietly going online.

The URL used is `http://127.0.0.1:9/...` (the discard port, nothing listening)
so that even with the guard removed the check fails fast and locally, without
sending a packet anywhere near a real filer or register. pytest-socket warns
as well as raising on a blocked call; the warning is filtered per test so the
suite's output stays clean.
"""
from __future__ import annotations

import socket

import pytest
from pytest_socket import SocketBlockedError

from company_corpus.config import Config
from company_corpus.http import Fetcher

BLOCKED_URL = "http://127.0.0.1:9/company-corpus-network-guard"


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_real_fetcher_get_is_blocked_by_the_socket_guard(monkeypatch):
    """A real Fetcher.get() cannot reach the network: unlike the retryable
    ``requests``/HTTP errors it catches, the guard's ``SocketBlockedError`` is
    not a ``requests.RequestException``, so it passes straight through
    ``get()``'s retry loop unwrapped -- attributing the failure to the guard,
    not mistaking it for a flaky endpoint."""
    # No backoff sleeps: a blocked socket must not turn one assertion into a
    # multi-second retry storm.
    monkeypatch.setattr("company_corpus.http.time.sleep", lambda _seconds: None)
    fetcher = Fetcher(Config(max_retries=1, timeout=1.0))

    with pytest.raises(SocketBlockedError):
        fetcher.get(BLOCKED_URL)


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_opening_a_bare_socket_is_blocked():
    """The guard sits at the `socket` layer, not on a requests-only stub:
    anything that opens a socket (a hand-rolled urllib call, a library phoning
    home) is stopped too."""
    with pytest.raises(SocketBlockedError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
