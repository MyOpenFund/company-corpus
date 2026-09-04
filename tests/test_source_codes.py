from __future__ import annotations

import pytest

from company_corpus.eu.acquire import COUNTRY_BACKENDS
from company_corpus.registers._common import _SOURCE_ID_SCHEME
from company_corpus.source_codes import CODE_RE, SOURCE_CODES, SourceCode, source_code_for


def test_exactly_23_codes():
    assert len(SOURCE_CODES) == 23


def test_every_code_matches_pattern():
    for code in SOURCE_CODES:
        assert CODE_RE.match(code), code


def test_codes_are_unique():
    # dict keys are already unique by construction; also check the .code field
    # on each entry agrees with its own key (no copy/paste drift).
    for key, entry in SOURCE_CODES.items():
        assert entry.code == key
    assert len(set(SOURCE_CODES)) == len(SOURCE_CODES)


def test_pillar_counts_partition_23():
    pillars = [entry.pillar for entry in SOURCE_CODES.values()]
    assert pillars.count("sec") == 1
    assert pillars.count("oam") == 14
    assert pillars.count("register") == 8
    assert len(pillars) == 23
    assert set(pillars) == {"sec", "oam", "register"}


def test_source_code_is_frozen_dataclass():
    entry = next(iter(SOURCE_CODES.values()))
    assert isinstance(entry, SourceCode)
    with pytest.raises(Exception):
        entry.code = "x"  # frozen -> raises FrozenInstanceError


@pytest.mark.parametrize("name", [cls.name for cls in COUNTRY_BACKENDS.values()])
def test_resolves_every_country_backend_class_name(name):
    assert source_code_for(name) in SOURCE_CODES


@pytest.mark.parametrize("name", ["euronext", "filings.xbrl.org", "sec", "esef"])
def test_resolves_known_producer_tags(name):
    assert source_code_for(name) in SOURCE_CODES


@pytest.mark.parametrize(
    "name",
    [*_SOURCE_ID_SCHEME.keys(), "ch"],
)
def test_resolves_every_register_source_tag(name):
    assert source_code_for(name) in SOURCE_CODES


def test_unknown_name_raises_key_error():
    with pytest.raises(KeyError):
        source_code_for("not-a-real-source")


def test_every_code_resolves_to_itself():
    for code in SOURCE_CODES:
        assert source_code_for(code) == code
