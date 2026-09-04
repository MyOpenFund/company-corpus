"""Packaging identity of company-corpus (renamed from the private bottom_up_corpus, clean break)."""
from __future__ import annotations
import subprocess, tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
# This module's own source necessarily contains the forbidden strings (they're
# quoted literally in the docstring/assertions above and the grep patterns
# below), so it must be excluded from the scan it defines.
SELF = Path(__file__).resolve().relative_to(REPO).as_posix()


def _require_git() -> None:
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=REPO, capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("not a git checkout")


def _pyproject() -> dict:
    with (REPO / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def test_distribution_and_script_names():
    p = _pyproject()["project"]
    assert p["name"] == "company-corpus"
    assert p["scripts"] == {"company-corpus": "company_corpus.cli:main"}


def test_package_dir_and_import_resolve_to_this_checkout():
    assert (REPO / "company_corpus" / "__init__.py").is_file()
    assert not (REPO / "bottom_up_corpus").exists()
    import company_corpus
    assert Path(company_corpus.__file__).resolve().is_relative_to(REPO)


def test_env_var_prefix_is_company_corpus_only():
    _require_git()
    out = subprocess.run(["git", "grep", "-l", "BOTTOM_UP_CORPUS_"], cwd=REPO, capture_output=True, text=True)
    hits = [ln for ln in out.stdout.splitlines() if ln != SELF]
    assert hits == [], out.stdout
    out = subprocess.run(["git", "grep", "-l", "bottom_up_corpus"], cwd=REPO, capture_output=True, text=True)
    hits = [ln for ln in out.stdout.splitlines() if ln != SELF]
    assert hits == [], out.stdout
    assert "COMPANY_CORPUS_CONTACT" in subprocess.run(["git", "grep", "-h", "COMPANY_CORPUS_CONTACT"], cwd=REPO, capture_output=True, text=True).stdout


def test_cli_prog_name_is_company_corpus():
    from company_corpus.cli import build_parser
    parser = build_parser()
    assert parser.prog == "company-corpus"
    assert "company-corpus" in parser.format_usage()
