"""Abstract discovery source.

Parallels ``cb_corpus.sources.base``. A source turns some EDGAR endpoint into a
stream of :class:`FilingRecord`. Discovery is idempotent (same input -> same
output) and never raises on a single bad item: transient failures are appended
to ``self.errors`` so an incomplete run is *detectable* rather than silently
partial.
"""

from __future__ import annotations

from abc import ABC
from collections.abc import Iterator

from ..config import Config
from ..http import Fetcher
from ..models import FilingRecord


class Source(ABC):
    """Base class for discovery sources."""

    name: str = "source"

    def __init__(self, fetcher: Fetcher | None = None, config: Config | None = None):
        self.config = config or (fetcher.config if fetcher else Config())
        self.fetcher = fetcher or Fetcher(self.config)
        self.errors: list[dict] = []

    def _record_error(self, context: str, url: str, error: Exception | str) -> None:
        self.errors.append(
            {
                "source": self.name,
                "context": context,
                "url": url,
                "error": str(error),
            }
        )

    def discover(self, *args, **kwargs) -> Iterator[FilingRecord]:  # pragma: no cover
        raise NotImplementedError


def cik_to_path_int(cik: str) -> str:
    """EDGAR Archives paths use the CIK without leading zeros (e.g. ``320193``)."""
    return str(int(cik))
