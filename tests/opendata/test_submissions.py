import hashlib
import json
from datetime import datetime, timezone

import pytest

from company_corpus.opendata.submissions import (
    META_SUFFIX,
    ArchiveCorrupt,
    ArchiveMissing,
    acceptance_times,
    archive_dir,
    load_archive,
)
from tests.opendata.factories import FETCHED, submissions_main, write_archive

CIK = "0000000042"


def test_archive_keeps_main_and_history_pages_with_sidecars(config):
    main = submissions_main(filings=[("0000000042-24-000001", "2024-07-30T20:06:22.000Z", "10-K")],
                            files=["CIK0000000042-submissions-001.json"])
    page = {"accessionNumber": ["0000000042-05-000009"],
            "acceptanceDateTime": ["2005-03-01T21:00:00.000Z"], "form": ["10-K"]}
    a = write_archive(config, CIK, main, pages={"CIK0000000042-submissions-001.json": page})
    d = archive_dir(config, CIK)
    assert sorted(p.name for p in d.iterdir()) == [
        "CIK0000000042-submissions-001.json", "CIK0000000042-submissions-001.json" + META_SUFFIX,
        "CIK0000000042.json", "CIK0000000042.json" + META_SUFFIX,
    ]
    meta = json.loads((d / ("CIK0000000042.json" + META_SUFFIX)).read_text())
    assert meta["url"] == "https://data.sec.gov/submissions/CIK0000000042.json"
    assert meta["sha256"] == hashlib.sha256((d / "CIK0000000042.json").read_bytes()).hexdigest()
    assert a.fetched_at == FETCHED and [f.name for f in a.files][0] == "CIK0000000042.json"
    assert a.files[0].rel_path == "raw/0000000042/submissions/CIK0000000042.json"
    assert acceptance_times(a) == {
        "0000000042-24-000001": datetime(2024, 7, 30, 20, 6, 22, tzinfo=timezone.utc),
        "0000000042-05-000009": datetime(2005, 3, 1, 21, 0, tzinfo=timezone.utc),
    }


def test_load_round_trips(config):
    write_archive(config, CIK, submissions_main())
    a = load_archive(config, CIK)
    assert a.main["name"] == "ACME CORP" and a.pages == []


def test_missing_archive(config):
    with pytest.raises(ArchiveMissing):
        load_archive(config, CIK)


def test_edited_file_is_corrupt(config):
    write_archive(config, CIK, submissions_main())
    f = archive_dir(config, CIK) / "CIK0000000042.json"
    f.write_text(f.read_text().replace("ACME", "EVIL"))
    with pytest.raises(ArchiveCorrupt, match="sha256"):
        load_archive(config, CIK)


def test_missing_history_page_is_corrupt(config):
    main = submissions_main(files=["CIK0000000042-submissions-001.json"])
    write_archive(config, CIK, main, pages={"CIK0000000042-submissions-001.json": {}})
    (archive_dir(config, CIK) / "CIK0000000042-submissions-001.json").unlink()
    with pytest.raises(ArchiveCorrupt):
        load_archive(config, CIK)


def test_hostile_page_name_is_corrupt_not_a_path(config):
    main = submissions_main(files=["../../escape.json"])
    with pytest.raises(ValueError):
        write_archive(config, CIK, main, pages={"../../escape.json": {}})
    assert not (config.data_dir / "escape.json").exists()


def test_a_failed_page_fetch_writes_nothing(config):
    from company_corpus.opendata.submissions import fetch_archive
    from tests.conftest import FakeFetcher

    main = submissions_main(files=["CIK0000000042-submissions-001.json"])
    fetcher = FakeFetcher({"submissions/CIK0000000042.json": json.dumps(main)}, config=config)
    with pytest.raises(RuntimeError, match="no route"):
        fetch_archive(config, CIK, fetcher, now=FETCHED)
    assert not archive_dir(config, CIK).exists()


def test_bad_acceptance_values_are_skipped_never_guessed(config):
    main = submissions_main(filings=[("a-1", "2024-07-30T20:06:22", "10-K"),   # no zone
                                     ("a-2", "not a date", "10-K"),
                                     ("", "2024-07-30T20:06:22.000Z", "10-K")])
    assert acceptance_times(write_archive(config, CIK, main)) == {}
