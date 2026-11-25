"""Thread-safe SEC client for live (no-vault) access.

The SEC contact comes only from the caller: there is no default and the
environment is never read. The contact is sent in the ``User-Agent`` and is
never logged.
"""

from __future__ import annotations

from typing import Protocol

import requests

from ..config import Config
from ..http import Fetcher

_UNDECLARED_MARKER = "undeclared automated tool"


class SecContactMissing(ValueError):
    """No SEC contact was supplied (blank or whitespace-only)."""


class SecRefused(RuntimeError):
    """The SEC refused the request as an undeclared automated tool."""


class SecUnavailable(RuntimeError):
    """The SEC stayed unreachable or throttled after every retry."""


class SecNotFound(LookupError):
    """The SEC answered 404 for the requested URL."""


class SecGet(Protocol):
    """Fetch the raw bytes of an SEC URL.

    ``max_age_s`` is a hint for caching wrappers; :class:`SecClient` ignores it.
    """

    def get_bytes(self, url: str, *, max_age_s: float | None) -> bytes: ...


class SecClient:
    """Throttled, retrying, thread-safe SEC client implementing :class:`SecGet`."""

    def __init__(self, contact: str, *, max_rps: float = 5.0, timeout_s: float = 30.0,
                 retries: int = 4, session: requests.Session | None = None):
        if not isinstance(contact, str) or not contact.strip():
            raise SecContactMissing("an SEC contact (e.g. an email address) is required")
        # ``Config.min_delay_seconds`` is derived as 1 / requests_per_second.
        config = Config(contact=contact.strip(), requests_per_second=max_rps,
                        max_retries=retries, timeout=timeout_s)
        self._fetcher = Fetcher(config, session, thread_safe=True)

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
            raise
        except (requests.ConnectionError, requests.Timeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ContentDecodingError) as exc:
            raise SecUnavailable(f"SEC unreachable: {type(exc).__name__}") from exc


def _body_text(resp) -> str:
    try:
        return resp.content.decode("utf-8", "replace").lower()
    except Exception:  # noqa: BLE001 - diagnostics only
        return ""
