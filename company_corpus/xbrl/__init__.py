"""Shared XBRL utilities, neutral to the pillar that consumes them.

Both the EU (ESEF) pillar and the national-register pillar parse IFRS/XBRL, so
these helpers live here rather than under ``eu`` to avoid a registers -> eu
layering leak:

* :mod:`~company_corpus.xbrl.oim` — flatten an OIM / xBRL-JSON report into the
  financials engine's point shape (``flatten_oim_json`` + period/unit helpers);
* :mod:`~company_corpus.xbrl.ifrs_concepts` — the ``ifrs-full`` concept pack
  (``IFRS_CONCEPTS``) mapped onto the same keys as the us-gaap pack.
"""

from __future__ import annotations

from .ifrs_concepts import IFRS_CONCEPTS, IFRS_CONCEPTS_BY_KEY
from .oim import flatten_oim_json, normalize_period, normalize_unit

__all__ = [
    "IFRS_CONCEPTS",
    "IFRS_CONCEPTS_BY_KEY",
    "flatten_oim_json",
    "normalize_period",
    "normalize_unit",
]
