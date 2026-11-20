"""The corpus vocabulary of the vault ``documents`` contract (``vocabulary.json``).

The vault ingestion checks every manifest row's codes against it (chantier 4, spec §3.2):
``source_code`` among the authorities of :mod:`company_corpus.source_codes` (all 23, so the
EU and register rows of a later chantier need no new vocabulary), ``doc_type`` among the
taxonomy codes of ``Config.vault_families`` (never F1), ``provenance`` among
:data:`SEC_PROVENANCES`, ``entity_scheme`` among the authorities' identifier schemes.

Generated from the code, never edited by hand: :func:`write_vocabulary` puts it at the data
root (the directory holding ``manifest/``), from ``migrate-contract`` and from every manifest
write. The runtime does not import the contract package; tests validate this file with it.
"""
from __future__ import annotations

import json

from .config import Config
from .source_codes import SOURCE_CODES
from .taxonomy import FormType

#: The contract this file declares (vault repo, ``contract/``, package ``vault-contract``).
CONTRACT_NAME = "documents"
CONTRACT_VERSION = "1.0.0"

#: How a FilingRecord was obtained (``FilingRecord.provenance``), every value a producer of
#: this corpus writes on a vault row: the EDGAR full index, full-text search, the
#: submissions API, a Wayback Machine capture. ``edgar_xbrl`` (F1) is not a vault row.
SEC_PROVENANCES: tuple[str, ...] = ("edgar_index", "edgar_fts", "edgar_submissions", "wayback")


def build_vocabulary(config: Config | None = None) -> dict:
    """The vocabulary document of corpus ``company`` (a JSON object)."""
    config = config or Config()
    families = set(config.vault_families)
    return {
        "contract": CONTRACT_NAME,
        "contract_version": CONTRACT_VERSION,
        "corpus": "company",
        "source_codes": [{"code": sc.code, "label": sc.authority}
                         for sc in SOURCE_CODES.values()],
        "doc_types": [{"code": ft.code, "label": ft.label, "family": ft.family}
                      for ft in FormType if ft.family in families],
        "provenances": list(SEC_PROVENANCES),
        "entity_schemes": sorted({sc.id_scheme for sc in SOURCE_CODES.values()}),
    }


def vocabulary_text(config: Config | None = None) -> str:
    return json.dumps(build_vocabulary(config), ensure_ascii=False, indent=2) + "\n"


def write_vocabulary(config: Config) -> bool:
    """Write ``vocabulary.json`` at the data root when its content changed; True if written."""
    from .storage import _atomic_write_text   # storage imports this module

    text = vocabulary_text(config)
    path = config.vocabulary_path
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    _atomic_write_text(path, text)
    return True
