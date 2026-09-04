from __future__ import annotations

import pytest

from company_corpus.entity import Entity, EntityRegistry


def _seeded(config) -> EntityRegistry:
    reg = EntityRegistry(config)
    reg.save([Entity(entity_id="alphabet", name="Alphabet / Google",
                     ciks=["1652044", "1288776"], note="2015 restructure")])
    return EntityRegistry(config).load()


def test_entity_normalizes_ciks():
    ent = Entity(entity_id="x", ciks=["1652044", 1288776])
    assert ent.ciks == ["0001652044", "0001288776"]
    assert ent.primary_cik == "0001652044"


def test_resolve_and_entity_id(config):
    reg = _seeded(config)
    ent = reg.resolve("1288776")
    assert ent is not None and ent.entity_id == "alphabet"
    assert reg.entity_id_for("1652044") == "alphabet"
    assert reg.entity_id_for("320193") == ""  # standalone


def test_expand_includes_siblings(config):
    reg = _seeded(config)
    # Querying the successor CIK expands to the predecessor too.
    assert set(reg.expand("1652044")) == {"0001652044", "0001288776"}
    # Standalone CIK expands to just itself.
    assert reg.expand("320193") == ["0000320193"]


def test_expand_all_dedups(config):
    reg = _seeded(config)
    out = reg.expand_all(["1652044", "320193", "1288776"])
    assert out.count("0001652044") == 1
    assert out.count("0001288776") == 1
    assert "0000320193" in out


def test_entities_iteration_is_deduped(config):
    reg = _seeded(config)
    ents = list(reg.entities())
    assert len(ents) == 1
    assert ents[0].entity_id == "alphabet"


def test_missing_registry_is_empty(config):
    reg = EntityRegistry(config).load()  # no file written
    assert reg.entity_id_for("320193") == ""
    assert reg.expand("320193") == ["0000320193"]


def test_duplicate_cik_across_entities_raises(config):
    reg = EntityRegistry(config)
    reg.add(Entity(entity_id="a", ciks=["111"]))
    with pytest.raises(ValueError, match="claimed by both"):
        reg.add(Entity(entity_id="b", ciks=["111"]))
    # The rejected add left the registry unchanged (no partial mutation). Inspect
    # internal state directly: a public query would lazy-reload from the (empty) file.
    assert reg._by_cik["0000000111"].entity_id == "a"
    assert "b" not in reg._by_id


def test_duplicate_entity_id_raises(config):
    reg = EntityRegistry(config)
    reg.add(Entity(entity_id="a", ciks=["111"]))
    with pytest.raises(ValueError, match="duplicate entity_id"):
        reg.add(Entity(entity_id="a", ciks=["222"]))


def test_load_rejects_conflicting_map(config):
    # Two committed lines claiming the same CIK must fail loudly, not silently
    # let the last line win and corrupt cross-CIK attribution.
    reg = EntityRegistry(config)
    reg.path.parent.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(
        '{"entity_id": "a", "ciks": ["0000000111"]}\n'
        '{"entity_id": "b", "ciks": ["0000000111"]}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="claimed by both"):
        EntityRegistry(config).load()
