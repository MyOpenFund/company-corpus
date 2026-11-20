from __future__ import annotations

import json

from vault_contract import golden, validate_vocabulary

from company_corpus.config import Config
from company_corpus.source_codes import SOURCE_CODES
from company_corpus.taxonomy import FormType
from company_corpus.vocabulary import (
    CONTRACT_NAME, CONTRACT_VERSION, SEC_PROVENANCES, build_vocabulary, write_vocabulary,
)


def test_the_generated_vocabulary_is_valid_for_the_contract():
    doc = build_vocabulary()
    assert validate_vocabulary(doc) == []
    assert (doc["contract"], doc["contract_version"], doc["corpus"]) == (
        "documents", "1.0.0", "company")
    assert (CONTRACT_NAME, CONTRACT_VERSION) == ("documents", "1.0.0")


def test_every_source_code_is_declared_with_its_scheme():
    doc = build_vocabulary()
    assert [e["code"] for e in doc["source_codes"]] == list(SOURCE_CODES)
    assert len(doc["source_codes"]) == 23
    assert {"code": "sec", "label": "SEC EDGAR"} in doc["source_codes"]
    assert doc["entity_schemes"] == sorted({sc.id_scheme for sc in SOURCE_CODES.values()})
    assert {"cik", "lei"} <= set(doc["entity_schemes"])


def test_doc_types_are_the_vault_families_only():
    doc = build_vocabulary()
    assert [e["code"] for e in doc["doc_types"]] == [
        ft.code for ft in FormType if ft.family in "ABCDE"]
    assert {"code": "A1", "label": "Annual report (10-K)", "family": "A"} in doc["doc_types"]
    assert "F1" not in {e["code"] for e in doc["doc_types"]}
    narrowed = build_vocabulary(Config(vault_families=("A",)))
    assert {e["family"] for e in narrowed["doc_types"]} == {"A"}


def test_provenances_are_the_sec_ones():
    assert build_vocabulary()["provenances"] == list(SEC_PROVENANCES) == [
        "edgar_index", "edgar_fts", "edgar_submissions", "wayback"]


def test_the_vault_golden_excerpt_is_part_of_this_vocabulary():
    excerpt, ours = golden.vocabulary("company"), build_vocabulary()
    for key in ("source_codes", "doc_types"):
        assert all(entry in ours[key] for entry in excerpt[key]), key
    for key in ("provenances", "entity_schemes"):
        assert set(excerpt[key]) <= set(ours[key]), key


def test_write_vocabulary_writes_it_once(tmp_path):
    config = Config(data_dir=tmp_path)
    assert write_vocabulary(config) is True
    assert json.loads(config.vocabulary_path.read_text(encoding="utf-8")) == build_vocabulary()
    mtime = config.vocabulary_path.stat().st_mtime_ns
    assert write_vocabulary(config) is False, "unchanged: not rewritten"
    assert config.vocabulary_path.stat().st_mtime_ns == mtime


def test_the_runtime_never_imports_the_contract_package():
    import company_corpus
    from pathlib import Path
    root = Path(company_corpus.__file__).parent
    offenders = [p for p in root.rglob("*.py") if "vault_contract" in p.read_text("utf-8")]
    assert offenders == []
