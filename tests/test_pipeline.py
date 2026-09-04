from __future__ import annotations

from datetime import date

from company_corpus.entity import Entity, EntityRegistry
from company_corpus.pipeline import discover_universe
from company_corpus.storage import Storage
from company_corpus.taxonomy import FULL_SCOPE


def test_dry_run_discovers_without_writing(apple_fetcher, config):
    report = discover_universe(["320193"], scope=FULL_SCOPE, dry_run=True,
                               config=config, fetcher=apple_fetcher)
    assert report.issuers == 1
    assert report.stats.added == 3   # 10-K, 10-Q, 8-K
    assert not config.manifest_file("320193").exists()


def test_write_persists_manifest(apple_fetcher, config):
    report = discover_universe(["320193"], scope=FULL_SCOPE, dry_run=False,
                               config=config, fetcher=apple_fetcher)
    assert report.stats.added == 3
    assert len(Storage(config).load_manifest("320193")) == 3


def test_convergence_second_round_adds_nothing(apple_fetcher, config):
    report = discover_universe(["320193"], scope=FULL_SCOPE, dry_run=False,
                               max_rounds=3, config=config, fetcher=apple_fetcher)
    # Round 1 adds 3; round 2 finds them all unchanged -> stop. So 2 rounds total.
    assert report.rounds == 2
    assert report.stats.added == 3
    assert report.stats.unchanged == 3


def test_since_filter_in_pipeline(apple_fetcher, config):
    report = discover_universe(["320193"], scope=FULL_SCOPE, since=date(2024, 6, 1),
                               dry_run=True, config=config, fetcher=apple_fetcher)
    assert report.stats.added == 2  # 8-K (2024-05-03) excluded


def test_missing_cik_surfaces_error(make_fetcher, config):
    report = discover_universe(["999999"], dry_run=False, config=config,
                               fetcher=make_fetcher({}))
    assert report.stats.added == 0
    assert len(report.errors) == 1
    assert config.discovery_errors_path.exists()


def test_alias_expansion_and_entity_stamp(apple_fetcher, config):
    # Entity groups Apple's CIK with a sibling CIK that has no fetcher route.
    reg = EntityRegistry(config)
    reg.save([Entity(entity_id="grp", name="Group", ciks=["320193", "789019"])])
    reg = EntityRegistry(config).load()

    report = discover_universe(["320193"], dry_run=False, config=config,
                               fetcher=apple_fetcher, entities=reg)
    # Input of one CIK expanded to the entity's two CIKs.
    assert report.issuers == 2
    # The unrouted sibling (789019) surfaces an error rather than failing silently.
    assert report.errors
    # Every persisted record is stamped with the canonical entity id.
    recs = Storage(config).load_manifest("320193")
    assert recs and all(r.entity_id == "grp" for r in recs.values())
