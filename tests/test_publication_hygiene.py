"""Publication gate: no process docs, no personal/employer references in the tree."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BENIGN = {"tests/fixtures/eu/it_companies.json"}  # an Italian issuer's name, legitimate data
FORBIDDEN = re.compile(rb"\bgenerali\b|onedrive|/Users/marc|Desktop/All CODING|jeulin|@gmail", re.I)
# This module's own source necessarily contains the forbidden substrings (they're
# in the regex pattern above), so it must be excluded from the scan it defines.
SELF = Path(__file__).resolve().relative_to(REPO).as_posix()


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, text=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("not a git checkout")
    return [p for p in out.stdout.split("\0") if p]


def test_no_process_docs_tracked():
    bad = [p for p in _tracked() if p.startswith(("docs/superpowers/", ".superpowers/"))
           or p in ("docs/ROADMAP.md", "docs/BE_STORI_STATUS.md", ".github/CODEOWNERS")]
    assert bad == []


def test_no_personal_or_employer_references_in_tracked_files():
    hits = []
    for rel in _tracked():
        if rel in BENIGN or rel == SELF:
            continue
        data = (REPO / rel).read_bytes()
        if FORBIDDEN.search(data):
            hits.append(rel)
    assert hits == []


def test_licences_present_and_scoped():
    lic = (REPO / "LICENSE").read_text()
    assert lic.startswith("MIT License") and "MyOpenFund" in lic
    dl = (REPO / "DATA_LICENSE").read_text()
    assert "CC BY 4.0" in dl and "tests/fixtures" in dl
    for cc in ("be", "dk", "ee", "eu", "fi", "lu", "sk", "uk"):
        assert f"`tests/fixtures/{cc}/`" in dl, cc
