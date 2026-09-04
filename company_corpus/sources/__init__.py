"""Discovery sources: how filings are *found* (before any download).

Each source yields :class:`~company_corpus.models.FilingRecord` objects and
records non-fatal failures on ``self.errors`` (audit trail), mirroring the
cb_corpus discovery contract.
"""

from __future__ import annotations

from .base import Source
from .edgar_index import EdgarFullIndex
from .edgar_submissions import EdgarSubmissions

__all__ = ["Source", "EdgarSubmissions", "EdgarFullIndex"]
