"""Thread-safe SEC client for live (no-vault) access.

The SEC contact comes only from the caller: there is no default and the
environment is never read. The contact is sent in the ``User-Agent`` and is
never logged.
"""

from __future__ import annotations

import math
from typing import Protocol

import requests

from ..config import Config
from ..http import Fetcher

_UNDECLARED_MARKER = "undeclared automated tool"

#: Default ``retry_after_max_s``: the longest server ``Retry-After`` (seconds) a request waits
#: for. A longer one raises :class:`SecUnavailable` at once rather than holding a live caller
#: for minutes (EDGAR can answer 429 with ``Retry-After: 600``). ``None`` removes the cap.
RETRY_AFTER_MAX_S = 30.0


class SecError(Exception):
    """Base of every error :class:`SecClient` raises for an SEC answer or its absence.

    Taxonomy (each also keeps a built-in base, so existing ``except`` clauses still match):

    * :class:`SecContactMissing` (``ValueError``): no usable contact; raised at construction;
    * :class:`SecRefused` (``RuntimeError``): HTTP 403 "undeclared automated tool" page: every
      request would be refused, so callers propagate it;
    * :class:`SecUnavailable` (``RuntimeError``): connection errors, timeouts, HTTP 429 and 5xx
      after every retry, or a ``Retry-After`` above ``retry_after_max_s``;
    * :class:`SecNotFound` (``LookupError``): HTTP 404 for this URL;
    * :class:`SecHttpError` (``RuntimeError``): any other non-2xx answer (400, 401, a 403
      without the undeclared-tool page, 410...), after a single request: it concerns this URL.
    """


class SecContactMissing(SecError, ValueError):
    """No SEC contact was supplied (blank or whitespace-only)."""


class SecRefused(SecError, RuntimeError):
    """The SEC refused the request as an undeclared automated tool."""


class SecUnavailable(SecError, RuntimeError):
    """The SEC stayed unreachable or throttled after every retry."""


class SecNotFound(SecError, LookupError):
    """The SEC answered 404 for the requested URL."""


class SecHttpError(SecError, RuntimeError):
    """The SEC answered another non-2xx status for the requested URL (``status``)."""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


class SecGet(Protocol):
    """Fetch the raw bytes of an SEC URL.

    ``max_age_s`` is a hint for caching wrappers; :class:`SecClient` ignores it.
    """

    def get_bytes(self, url: str, *, max_age_s: float | None) -> bytes: ...


class SecClient:
    """Throttled, retrying, thread-safe SEC client implementing :class:`SecGet`."""

    def __init__(self, contact: str, *, max_rps: float = 5.0, timeout_s: float = 30.0,
                 retries: int = 4, retry_after_max_s: float | None = RETRY_AFTER_MAX_S,
                 session: requests.Session | None = None):
        if not isinstance(contact, str) or not contact.strip():
            raise SecContactMissing("an SEC contact (e.g. an email address) is required")
        if "\r" in contact or "\n" in contact:
            raise SecContactMissing("the SEC contact must not contain line breaks")
        if not max_rps > 0:
            raise ValueError("max_rps must be > 0")
        if retries < 0:
            raise ValueError("retries must be >= 0")
        if not timeout_s > 0:
            raise ValueError("timeout_s must be > 0")
        if retry_after_max_s is not None and not (math.isfinite(retry_after_max_s)
                                                  and retry_after_max_s >= 0):
            raise ValueError("retry_after_max_s must be a finite number >= 0, or None")
        # ``Config.min_delay_seconds`` is derived as 1 / requests_per_second.
        config = Config(contact=contact.strip(), requests_per_second=max_rps,
                        max_retries=retries, timeout=timeout_s)
        self._fetcher = Fetcher(config, session, thread_safe=True,
                                 throttle_key=lambda url: "sec",  # one cap across SEC hosts
                                 retry_after_max_s=retry_after_max_s)

    def get_bytes(self, url: str, *, max_age_s: float | None = None) -> bytes:
        try:
            return self._fetcher.get(url).content
        except requests.HTTPError as exc:
            resp = exc.response
            status = getattr(resp, "status_code", None)
            if status == 404:
                raise SecNotFound(url) from exc
            if status == 403 and _UNDECLARED_MARKER in _body_text(resp):
                raise SecRefused(
                    "SEC refused the request as an undeclared automated tool; "
                    "check the declared contact") from exc
            if status == 429 or (status is not None and 500 <= status <= 599):
                raise SecUnavailable(f"SEC unavailable (HTTP {status}) after retries") from exc
            raise SecHttpError(status, f"SEC answered HTTP {status} for {url}") from exc
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError) as exc:
            raise SecUnavailable(f"SEC unreachable: {type(exc).__name__}") from exc


def _body_text(resp) -> str:
    try:
        return resp.content.decode("utf-8", "replace").lower()
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""
