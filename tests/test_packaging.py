"""Packaging identity of company-corpus (renamed from the private bottom_up_corpus, clean break).

Published on PyPI as `myopenfund-company-corpus`; the import name (`company_corpus`), the command
(`company-corpus`) and the repository name never change.
"""
from __future__ import annotations
import importlib.metadata
import re
import subprocess, tomllib
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

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


DIST = "myopenfund-company-corpus"
CREDIT = (
    "Created and maintained by [Marc Jeulin](https://github.com/jeulinmarc), "
    "part of [MyOpenFund](https://github.com/MyOpenFund)."
)
RELEASE = REPO / ".github" / "workflows" / "release.yml"
TESTS = REPO / ".github" / "workflows" / "tests.yml"
BLOB = "https://github.com/MyOpenFund/company-corpus/blob/main/"


def _workflow(path: Path) -> dict:
    # PyYAML reads the bare key `on` as the boolean True.
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))
    return data


def _upload_index(steps: list) -> list:
    return [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/upload-artifact@")]


def test_distribution_and_script_names():
    p = _pyproject()["project"]
    assert p["name"] == DIST
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


def test_dynamic_version_from_init():
    import company_corpus
    cfg = _pyproject()
    project = cfg["project"]
    assert "version" in project["dynamic"]
    assert "version" not in project
    assert cfg["tool"]["setuptools"]["dynamic"]["version"] == {"attr": "company_corpus.__version__"}
    assert cfg["build-system"]["requires"] == ["setuptools>=77"]
    assert company_corpus.__version__ == "0.1.0"


def test_installed_version_matches_init():
    import company_corpus
    try:
        installed = importlib.metadata.version(DIST)
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("the project is run from a checkout, not installed (CI installs requirements only)")
    assert installed == company_corpus.__version__


def test_credit_metadata():
    project = _pyproject()["project"]
    assert project["authors"] == [{"name": "Marc Jeulin"}]
    assert project["maintainers"] == [{"name": "Marc Jeulin"}]
    assert "myopenfund" in project["keywords"]
    urls = project["urls"]
    assert urls["Organization"] == "https://github.com/MyOpenFund"
    assert urls["Homepage"] == "https://github.com/MyOpenFund/company-corpus"
    assert urls["Source"] == "https://github.com/MyOpenFund/company-corpus"
    assert urls["Issues"] == "https://github.com/MyOpenFund/company-corpus/issues"
    assert "Changelog" not in urls  # no CHANGELOG in this repo
    assert project["readme"] == "README.md"


def test_license_spdx_files_and_classifiers():
    cfg = _pyproject()
    project = cfg["project"]
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE", "DATA_LICENSE"]
    assert not [c for c in project["classifiers"] if c.startswith("License ::")]
    assert "Development Status :: 3 - Alpha" in project["classifiers"]
    for version in ("3.11", "3.12", "3.13"):
        assert f"Programming Language :: Python :: {version}" in project["classifiers"]


def test_license_copyright():
    assert "Copyright (c) 2026 Marc Jeulin" in (REPO / "LICENSE").read_text(encoding="utf-8")
    assert (REPO / "DATA_LICENSE").is_file()


def test_readme_credit_install_and_migration():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "# company-corpus"
    assert [line for line in lines[1:] if line.strip()][0] == CREDIT
    assert f'pip install "{DIST}"' in text
    assert f'pip install "{DIST}[store]"' in text
    assert "pip install \"company-corpus" not in text
    assert "pip uninstall -y company-corpus" in text
    assert "git+https://github.com/MyOpenFund/company-corpus" in text
    assert "**Working across repos**" in text


def test_readme_links_are_absolute():
    # README.md is the PyPI description: relative links and anchors would 404 there.
    text = (REPO / "README.md").read_text(encoding="utf-8")
    assert "](#" not in text
    targets = re.findall(r"\]\(([^)\s]+)\)", text)
    assert targets
    relative = [t for t in targets if not t.startswith("https://")]
    assert not relative, relative
    assert f"{BLOB}docs/FINANCIALS.md" in text
    assert f"{BLOB}LICENSE" in text


def test_store_extra_is_capped_and_published():
    extra = _pyproject()["project"]["optional-dependencies"]["store"]
    assert extra == ["myopenfund-open-data-store[writer]>=0.1.0,<0.2"]


def test_no_git_urls_in_metadata():
    project = _pyproject()["project"]
    deps = list(project["dependencies"])
    for extra in project["optional-dependencies"].values():
        deps += extra
    assert not [d for d in deps if "git+" in d or "@" in d]


def test_sibling_dependencies_are_capped():
    project = _pyproject()["project"]
    reqs = [Requirement(d) for d in project["dependencies"]]
    for extra in project["optional-dependencies"].values():
        reqs += [Requirement(d) for d in extra]
    siblings = [r for r in reqs if r.name.startswith("myopenfund-")]
    assert siblings
    for r in siblings:
        assert {(s.operator, s.version) for s in r.specifier} == {(">=", "0.1.0"), ("<", "0.2")}, r


def test_requirements_files_pin_siblings_exactly():
    store = (REPO / "requirements-store.txt").read_text(encoding="utf-8")
    dev = (REPO / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "myopenfund-open-data-store[publisher]==0.1.0" in store
    assert "myopenfund-vault-contract==1.1.0" in dev
    assert "git+" not in store and "git+" not in dev
    assert any(line.startswith("pyyaml") for line in dev.splitlines())


def test_install_hints_name_the_published_distribution():
    import company_corpus.opendata as opendata
    from company_corpus.opendata import batches

    hint = f"pip install '{DIST}[store]'"
    assert hint in Path(batches.__file__).read_text(encoding="utf-8")
    assert f"``{DIST}[store]``" in (opendata.__doc__ or "")
    for module in (batches, opendata):
        assert "'company-corpus[" not in Path(module.__file__).read_text(encoding="utf-8")
        assert "``company-corpus[" not in Path(module.__file__).read_text(encoding="utf-8")


def test_dependabot_covers_pip_and_github_actions_weekly():
    config = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    assert sorted((u["package-ecosystem"], u["schedule"]["interval"]) for u in config["updates"]) == [
        ("github-actions", "weekly"), ("pip", "weekly")
    ]


def test_release_workflow_publishes_through_environment():
    workflow = _workflow(RELEASE)
    assert workflow["permissions"] == {}
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert list(workflow["on"]["workflow_dispatch"]["inputs"]) == ["target"]
    publish = workflow["jobs"]["publish"]
    assert publish["environment"] == (
        "${{ github.event_name == 'workflow_dispatch' && 'testpypi' || 'pypi' }}"
    )
    assert publish["permissions"] == {"id-token": "write"}
    with_id_token = [
        name for name, job in workflow["jobs"].items() if "id-token" in job.get("permissions", {})
    ]
    assert with_id_token == ["publish"]
    assert publish["needs"] in ("build", ["build"])


def test_actions_pinned_by_sha():
    package = yaml.safe_dump(_workflow(TESTS)["jobs"]["package"])
    for text in (RELEASE.read_text(encoding="utf-8"), package):
        uses = re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", text, re.M)
        assert uses
        for ref in uses:
            assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), ref


def test_tag_check_present_with_prefix_v():
    steps = _workflow(RELEASE)["jobs"]["build"]["steps"]
    checks = [i for i, s in enumerate(steps) if "tag_version" in s.get("run", "") and "exit 1" in s["run"]]
    assert len(checks) == 1
    assert steps[checks[0]]["if"] == "github.event_name == 'push'"
    assert "${GITHUB_REF_NAME#v}" in steps[checks[0]]["run"]
    assert DIST in steps[checks[0]]["run"]
    upload = _upload_index(steps)
    assert upload and checks[0] < upload[0]


def test_build_checks_tag_is_on_default_branch():
    steps = _workflow(RELEASE)["jobs"]["build"]["steps"]
    checks = [i for i, s in enumerate(steps) if "merge-base --is-ancestor" in s.get("run", "")]
    assert len(checks) == 1
    assert "origin/main" in steps[checks[0]]["run"]
    assert steps[checks[0]]["if"] == "github.event_name == 'push'"
    assert steps[0]["with"]["fetch-depth"] == 0
    upload = _upload_index(steps)
    assert upload and checks[0] < upload[0]


def test_testpypi_publish_skips_existing_but_pypi_does_not():
    steps = {s["name"]: s for s in _workflow(RELEASE)["jobs"]["publish"]["steps"] if "name" in s}
    assert steps["Publish to TestPyPI"]["with"]["skip-existing"] is True
    assert "skip-existing" not in steps["Publish to PyPI"].get("with", {})


def test_concurrency_group():
    assert _workflow(RELEASE)["concurrency"]["group"] == "release-${{ github.ref }}"


def test_github_release_is_push_only_and_verifies_tag():
    job = _workflow(RELEASE)["jobs"]["github-release"]
    assert job["if"] == "github.event_name == 'push'"
    assert job["permissions"] == {"contents": "write"}
    runs = " ".join(s.get("run", "") for s in job["steps"])
    assert "--verify-tag" in runs
    assert "--generate-notes" in runs


def test_smoke_checks_cli_version_and_reads_packaged_data():
    for path in (RELEASE, TESTS):
        text = path.read_text(encoding="utf-8")
        assert '/bin/company-corpus" --version' in text, path
        assert "company_corpus.concepts" in text, path
        assert "importlib.resources" in text, path


def test_tests_workflow_keeps_its_jobs_adds_package_and_runs_weekly():
    workflow = _workflow(TESTS)
    assert {"unit", "store", "package"} <= set(workflow["jobs"])
    assert workflow["on"]["schedule"] == [{"cron": workflow["on"]["schedule"][0]["cron"]}]
    assert len(workflow["on"]["schedule"][0]["cron"].split()) == 5
    runs = " ".join(s.get("run", "") for s in workflow["jobs"]["package"]["steps"])
    assert "build==1.6.1 twine==7.0.0" in runs
    assert "twine check --strict" in runs
