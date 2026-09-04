"""Shared GLEIF single-LEI record fetch + field parsing.

Two resolvers look up a *single* LEI record at
``api.gleif.org/api/v1/lei-records/{lei}`` and read the same identity fields
(``entity.legalName.name`` / ``entity.legalAddress.country`` /
``entity.registeredAs``):

* :mod:`company_corpus.eu.entities` — resolves an issuer to an ``Entity``
  (falls back to ``headquartersAddress.country``; ignores ``registeredAs``);
* :mod:`company_corpus.registers.identity` — resolves an LEI to a national
  register key via ``registeredAs`` (no country fallback; country-guarded).

This module owns the *fetch* and the *field access* so the two can't drift.
It is deliberately low-level and no-guess: :func:`fetch_gleif_record` maps any
GLEIF failure (or an unknown LEI) to ``None``, and :func:`parse_gleif_record`
returns the **raw** JSON field values with no fallbacks or coercion, leaving
each caller's own country-fallback / country-guard policy exactly where it was.
"""

from __future__ import annotations

# Single-record endpoint. The LEI is substituted verbatim (unquoted), matching
# both historical call sites.
GLEIF_RECORD_URL = "https://api.gleif.org/api/v1/lei-records/{lei}"


def fetch_gleif_record(lei: str, *, fetcher) -> dict | None:
    """Return a single LEI record's ``data`` object, or ``None``.

    Scoped try/except: any GLEIF network / HTTP / JSON failure maps to ``None``
    so the caller treats the entity as unresolved and never guesses. ``None`` is
    also returned when the response carries no ``data`` (unknown LEI).
    """
    try:
        raw = fetcher.get_json(GLEIF_RECORD_URL.format(lei=lei))
    except Exception:  # noqa: BLE001 — GLEIF network/HTTP failure -> unresolved
        return None
    return (raw or {}).get("data") or None


def parse_gleif_record(attributes: dict) -> dict:
    """Read the identity fields from a GLEIF record's ``attributes`` mapping.

    ``attributes`` is the ``data.attributes`` object of a single record *or* of
    a search-result row (both share the schema). The values are returned raw,
    with no fallback or coercion, so each caller keeps its own no-guess policy:

    * ``lei``            — top-level ``lei``                     (``""`` if absent)
    * ``name``           — ``entity.legalName.name``            (``""`` if absent)
    * ``legal_country``  — ``entity.legalAddress.country``      (``None`` if absent)
    * ``hq_country``     — ``entity.headquartersAddress.country`` (``None`` if absent)
    * ``registered_as``  — ``entity.registeredAs``              (``None`` if absent)
    """
    ent = attributes.get("entity", {})
    return {
        "lei": attributes.get("lei", ""),
        "name": (ent.get("legalName") or {}).get("name", ""),
        "legal_country": (ent.get("legalAddress") or {}).get("country"),
        "hq_country": (ent.get("headquartersAddress") or {}).get("country"),
        "registered_as": ent.get("registeredAs"),
    }
