"""Guards against the sister repo's mistake: tests that silently wrote phantom
run-reports into the checked-out repo's own ``data/runs.jsonl``.

``tests/conftest.py`` must point ``COMPANY_DATA_DIR`` at a session-scoped tmp
directory for the whole test session, so nothing under test can ever resolve
``_runs_path()`` back into this repository's working tree.
"""
from __future__ import annotations

import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def test_repo_data_dir_has_no_runs_file():
    assert not (REPO / "data" / "runs.jsonl").exists()
    override = os.environ.get("COMPANY_DATA_DIR")
    assert override is not None
    assert REPO not in Path(override).resolve().parents and Path(override).resolve() != REPO
