from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .documents import Document
from .entities import Entity


@dataclass
class IssuerRef:
    lei: str | None
    name: str
    country: str
    native_id: str


class OamSource(ABC):
    """One national OAM (or a complementary aggregator) as a pluggable backend."""
    country: str = "??"
    name: str = "oam"

    def __init__(self, fetcher=None, config=None):
        from ..config import Config
        from ..http import Fetcher
        self.config = config or (getattr(fetcher, "config", None) if fetcher else None) or Config()
        self.fetcher = fetcher or Fetcher(self.config)
        self.errors: list[dict] = []

    def _record_error(self, context, url, error):
        self.errors.append({"source": self.name, "context": context, "url": url, "error": str(error)})

    def list_issuers(self) -> list[IssuerRef]:
        """Enumerate all known issuers for this OAM.

        Default: return empty — full enumeration is a scale-up concern for most
        backends (discovery is driven per-entity via :meth:`discover`).  Backends
        that can cheaply enumerate (e.g. OamFI's embedded company list) override it.
        """
        return []

    @abstractmethod
    def discover(self, entity: Entity) -> list[Document]: ...
    # Downloading is centralised in eu/download.py (not on the backend).
