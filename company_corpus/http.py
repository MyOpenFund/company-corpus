"""HTTP fetcher with SEC fair-access compliance.

Parallels ``cb_corpus.http``. Provides a small :class:`Fetcher` that:

* sends a declared, contact-carrying ``User-Agent`` (SEC requirement),
* throttles per host to stay at/under the SEC's 10 req/s ceiling,
* retries *transient* failures only (connection errors, timeouts, HTTP 429 and
  5xx) with exponential backoff — any other 4xx is a definitive answer and
  raises after a single request, so a 404 never costs the host four requests,
* decodes text bodies by the declared charset; when none is declared it tries
  strict UTF-8 first and otherwise keeps the header default (``requests``
  would default ``text/*`` to ISO-8859-1 outright and turn UTF-8 pages into
  mojibake; no charset sniffing, which mis-reads real latin-1 pages),
* streams large bodies (complete submissions can be many MB).
"""

from __future__ import annotations

import random
import time
from typing import Callable
from urllib.parse import urlsplit

import requests

from .config import Config


class Fetcher:
    """Polite, throttled HTTP client shared across discovery and download.

    Not thread-safe: the per-host throttle state is unguarded, so a single
    ``Fetcher`` is meant to be reused *sequentially* within one thread. The
    pipeline is single-threaded, so no lock is needed; if concurrent crawling is
    ever added, give each worker its own ``Fetcher`` or guard ``_throttle``.
    """

    def __init__(self, config: Config | None = None, session: requests.Session | None = None):
        self.config = config or Config()
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.config.user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        # TLS verification applies to every request on the session (incl. streamed
        # downloads). Disable only behind a trusted MITM/SSL-inspection proxy.
        self.session.verify = self.config.verify_tls
        if not self.config.verify_tls:
            # Otherwise urllib3 emits an InsecureRequestWarning on every request.
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # Last-request timestamp per host, for spacing.
        self._last_request: dict[str, float] = {}

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        delay = self.config.min_delay_seconds
        if delay <= 0:
            return
        last = self._last_request.get(host)
        if last is not None:
            wait = delay - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_request[host] = time.monotonic()

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """The retry predicate: is ``exc`` worth another request?

        Only failures that a later attempt can plausibly cure: a connection
        error or timeout (``requests.ConnectionError`` / ``requests.Timeout``),
        HTTP 429 (throttled) and any 5xx. Every other 4xx (404, 403, 400, …) is
        the server's definitive answer, and a malformed request (missing
        schema, invalid URL, …) is a caller bug: retrying either only burns the
        host's quota and the backoff time — contra the README's "Fair access".
        """
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(exc, requests.HTTPError):
            status = getattr(exc.response, "status_code", None)
            return status == 429 or (status is not None and 500 <= status <= 599)
        return False

    def _send(self, url: str, do_request: Callable[[], requests.Response]) -> requests.Response:
        """Run ``do_request()`` (one attempt on the session) under throttle +
        retry; return the successful response.

        A 429 / 5xx status is raised as :class:`requests.HTTPError` (with the
        response attached, so ``Retry-After`` is honoured) and retried up to
        ``config.max_retries`` times; any other non-2xx status raises through
        ``raise_for_status`` after this single attempt. Transport exceptions go
        through :meth:`_is_transient`: retried when transient, raised at once
        otherwise. Whatever failed last is what the caller sees.
        """
        for attempt in range(self.config.max_retries + 1):
            self._throttle(url)
            try:
                resp = do_request()
                status = resp.status_code
                if status == 429 or 500 <= status <= 599:
                    raise requests.HTTPError(f"{status} for {url}", response=resp)
                resp.raise_for_status()
                return resp
            except requests.RequestException as exc:
                if attempt < self.config.max_retries and self._is_transient(exc):
                    time.sleep(self._backoff_seconds(attempt, exc))
                    continue
                raise
        raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover

    def get(self, url: str, *, stream: bool = False, timeout: float | None = None,
            headers: dict | None = None, params: dict | None = None) -> requests.Response:
        """GET ``url`` with throttling + retries; returns the response.

        Retries only transient failures (connection errors, timeouts, HTTP 429
        and 5xx — see :meth:`_is_transient`); any other 4xx raises
        ``requests.HTTPError`` after exactly one request. Raises the last
        exception if every attempt fails. ``headers`` are merged into this
        request only (per-request, NOT onto the shared session). ``params`` are
        URL-encoded onto the query string by ``requests``.
        """
        return self._send(url, lambda: self.session.get(
            url,
            stream=stream,
            timeout=timeout or self.config.timeout,
            headers=headers,
            params=params,
        ))

    _BACKOFF_CAP_SECONDS = 30.0

    def _backoff_seconds(self, attempt: int, exc: Exception) -> float:
        """How long to wait before the next retry.

        Honors a server ``Retry-After`` (delta-seconds form) when present --
        EDGAR sends it on 429/503 -- otherwise exponential backoff (2**attempt),
        capped and jittered (50-100%) so concurrent clients hitting the same
        throttle don't retry in lockstep (thundering herd).
        """
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
        retry_after = headers.get("Retry-After") if hasattr(headers, "get") else None
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except (TypeError, ValueError):
                pass  # HTTP-date form is not parsed; fall through to backoff
        base = min(2.0 ** attempt, self._BACKOFF_CAP_SECONDS)
        return base * (0.5 + random.random() * 0.5)

    @staticmethod
    def _decode(resp: requests.Response) -> str:
        """Decode a text body: the declared charset when the ``Content-Type``
        header carries one; otherwise strict UTF-8 if the bytes are valid
        UTF-8, else the encoding ``requests`` derived from the headers
        (ISO-8859-1 for ``text/*``), else UTF-8 when there is no header at all.

        ``requests`` fills ``resp.encoding`` with ISO-8859-1 for any ``text/*``
        body that declares no charset (the RFC 2616 default), so a UTF-8 page
        from a regulator that omits the charset used to come back as
        ``"cafÃ©"``. Charset sniffing (``apparent_encoding``) is deliberately
        not used: it mis-detects real latin-1/cp1252 pages as windows-1250 and
        a large mostly-ASCII ``text/plain`` submission with one accented byte
        as Big5 -- bodies the header default decodes correctly. A *declared*
        charset is never overridden: the header is a statement, not a guess.
        """
        headers = getattr(resp, "headers", None)
        ctype = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
        if "charset" not in (ctype or "").lower():
            try:
                resp.content.decode("utf-8")
            except UnicodeDecodeError:
                if resp.encoding is None:
                    resp.encoding = "utf-8"
            else:
                resp.encoding = "utf-8"
        return resp.text

    def get_text(self, url: str, *, timeout: float | None = None,
                 headers: dict | None = None, params: dict | None = None) -> str:
        """Fetch and decode a text body (see :meth:`_decode` for the charset rule)."""
        return self._decode(self.get(url, timeout=timeout, headers=headers, params=params))

    def get_json(self, url: str, *, timeout: float | None = None, headers: dict | None = None,
                 params: dict | None = None):
        """Fetch and parse a JSON body (used by data.sec.gov endpoints).

        ``headers`` are merged into this request only (not the shared session)."""
        return self.get(url, timeout=timeout, headers=headers, params=params).json()

    def post_json(self, url: str, json_body, *, timeout: float | None = None,
                  headers: dict | None = None):
        """POST ``url`` with a JSON body; returns the parsed JSON response.

        Applies the same throttle, retry, and raise-for-status policy as
        :meth:`get` (transient failures only). ``headers`` are merged into this
        request only (not onto the shared session).
        """
        return self._send(url, lambda: self.session.post(
            url,
            json=json_body,
            timeout=timeout or self.config.timeout,
            headers=headers,
        )).json()

    def post_text(self, url: str, data, *, timeout: float | None = None) -> str:
        """POST ``url`` with a form-encoded body; returns the decoded text response.

        Applies the same throttle, retry, and raise-for-status policy as
        :meth:`get` (transient failures only) and the charset rule of
        :meth:`_decode`. Used by the stateful Wicket scrape (Bundesanzeiger)
        that drives its search via a form-encoded POST rather than JSON.
        """
        return self._decode(self._send(url, lambda: self.session.post(
            url,
            data=data,
            timeout=timeout or self.config.timeout,
        )))

    def download(self, url: str, dest, *, chunk_size: int = 1 << 16) -> int:
        """Stream ``url`` to ``dest`` (a path-like). Returns bytes written."""
        from pathlib import Path

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        resp = self.get(url, stream=True, timeout=self.config.download_timeout)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
        return written
