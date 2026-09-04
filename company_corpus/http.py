"""HTTP fetcher with SEC fair-access compliance.

Parallels ``cb_corpus.http``. Provides a small :class:`Fetcher` that:

* sends a declared, contact-carrying ``User-Agent`` (SEC requirement),
* throttles per host to stay at/under the SEC's 10 req/s ceiling,
* retries transient failures with exponential backoff,
* streams large bodies (complete submissions can be many MB).
"""

from __future__ import annotations

import random
import time
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

    def get(self, url: str, *, stream: bool = False, timeout: float | None = None,
            headers: dict | None = None, params: dict | None = None) -> requests.Response:
        """GET ``url`` with throttling + retries; returns the response.

        Raises the last exception (or ``requests.HTTPError``) if all attempts
        fail. 429/503 responses are treated as retryable. ``headers`` are merged
        into this request only (per-request, NOT onto the shared session).
        ``params`` are URL-encoded onto the query string by ``requests``.
        """
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._throttle(url)
            try:
                resp = self.session.get(
                    url,
                    stream=stream,
                    timeout=timeout or self.config.timeout,
                    headers=headers,
                    params=params,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{resp.status_code} for {url}", response=resp)
                resp.raise_for_status()
                return resp
            except (requests.RequestException, requests.HTTPError) as exc:
                last_exc = exc
                if attempt < self.config.max_retries:
                    time.sleep(self._backoff_seconds(attempt, exc))
                    continue
                raise
        # Unreachable, but keeps type-checkers happy.
        assert last_exc is not None
        raise last_exc

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

    def get_text(self, url: str, *, timeout: float | None = None,
                 headers: dict | None = None, params: dict | None = None) -> str:
        """Fetch and decode a text body."""
        resp = self.get(url, timeout=timeout, headers=headers, params=params)
        resp.encoding = resp.encoding or "utf-8"
        return resp.text

    def get_json(self, url: str, *, timeout: float | None = None, headers: dict | None = None,
                 params: dict | None = None):
        """Fetch and parse a JSON body (used by data.sec.gov endpoints).

        ``headers`` are merged into this request only (not the shared session)."""
        return self.get(url, timeout=timeout, headers=headers, params=params).json()

    def post_json(self, url: str, json_body, *, timeout: float | None = None,
                  headers: dict | None = None):
        """POST ``url`` with a JSON body; returns the parsed JSON response.

        Applies the same throttle, retry, and raise-for-status policy as
        :meth:`get`.  429 / 5xx responses are treated as retryable. ``headers``
        are merged into this request only (not onto the shared session).
        """
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._throttle(url)
            try:
                resp = self.session.post(
                    url,
                    json=json_body,
                    timeout=timeout or self.config.timeout,
                    headers=headers,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{resp.status_code} for {url}", response=resp)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, requests.HTTPError) as exc:
                last_exc = exc
                if attempt < self.config.max_retries:
                    time.sleep(self._backoff_seconds(attempt, exc))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def post_text(self, url: str, data, *, timeout: float | None = None) -> str:
        """POST ``url`` with a form-encoded body; returns the decoded text response.

        Applies the same throttle, retry, and raise-for-status policy as
        :meth:`get`.  429 / 5xx responses are treated as retryable. Used by the
        stateful Wicket scrape (Bundesanzeiger) that drives its search via a
        form-encoded POST rather than JSON.
        """
        last_exc: Exception | None = None
        for attempt in range(self.config.max_retries + 1):
            self._throttle(url)
            try:
                resp = self.session.post(
                    url,
                    data=data,
                    timeout=timeout or self.config.timeout,
                )
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise requests.HTTPError(f"{resp.status_code} for {url}", response=resp)
                resp.raise_for_status()
                resp.encoding = resp.encoding or "utf-8"
                return resp.text
            except (requests.RequestException, requests.HTTPError) as exc:
                last_exc = exc
                if attempt < self.config.max_retries:
                    time.sleep(self._backoff_seconds(attempt, exc))
                    continue
                raise
        assert last_exc is not None
        raise last_exc

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
