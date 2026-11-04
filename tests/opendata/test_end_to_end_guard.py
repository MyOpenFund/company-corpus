"""REQUIRE_DUCKLAKE=1 (the CI ``store`` job) turns the end-to-end test's skip into a failure."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NO_TESTS_COLLECTED = 5  # what pytest returns when the only module skipped itself

# Shadows the real duckdb: the extension can neither be installed nor loaded.
BROKEN_DUCKDB = """
class Error(Exception):
    pass

class _Connection:
    def execute(self, sql):
        raise Error("ducklake extension unavailable")

def connect():
    return _Connection()
"""


def _run_end_to_end(tmp_path, *, require) -> subprocess.CompletedProcess:
    (tmp_path / "duckdb.py").write_text(BROKEN_DUCKDB, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    env.pop("REQUIRE_DUCKLAKE", None)
    if require:
        env["REQUIRE_DUCKLAKE"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/opendata/test_end_to_end.py"],
        cwd=REPO, env=env, capture_output=True, text=True, check=False)


def test_without_the_variable_a_load_failure_skips(tmp_path):
    res = _run_end_to_end(tmp_path, require=False)
    assert res.returncode == NO_TESTS_COLLECTED, res.stdout + res.stderr
    assert "skipped" in res.stdout


def test_with_the_variable_a_load_failure_fails(tmp_path):
    pytest.importorskip("myopendata")
    res = _run_end_to_end(tmp_path, require=True)
    assert res.returncode not in (0, NO_TESTS_COLLECTED), res.stdout + res.stderr
    assert "ducklake extension unavailable" in res.stdout
