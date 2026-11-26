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
from packaging.version import Version

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
    # the attribute the build reads is the single source of the version: a valid PEP 440 release
    assert str(Version(company_corpus.__version__)) == company_corpus.__version__


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


def _next_breaking(floor: str) -> Version:
    """The first version that may break a caller of `floor`: 0.m.x -> 0.(m+1); M.x.y -> M+1."""
    v = Version(floor)
    return Version(f"0.{v.minor + 1}") if v.major == 0 else Version(str(v.major + 1))


def _assert_floor_and_cap(r: Requirement) -> None:
    """Exactly `>=X.Y.Z` and the next breaking version as cap, consistent with the floor."""
    ops = {s.operator: Version(s.version) for s in r.specifier}
    assert len(r.specifier) == 2 and set(ops) == {">=", "<"}, r
    assert ops["<"] == _next_breaking(str(ops[">="])), r


def test_cap_rule_accepts_the_next_breaking_version_only():
    import pytest

    for ok in ("x>=0.1.0,<0.2", "x>=0.4.2,<0.5", "x>=1.1.0,<2", "x>=2.0,<3"):
        _assert_floor_and_cap(Requirement(ok))
    for bad in ("x>=0.1.0", "x>=0.1.0,<0.3", "x>=1.1.0,<1.2", "x==0.1.0", "x>=0.1.0,<0.2,!=0.1.5"):
        with pytest.raises(AssertionError):
            _assert_floor_and_cap(Requirement(bad))


def test_store_extra_is_capped_and_published():
    extra = _pyproject()["project"]["optional-dependencies"]["store"]
    assert len(extra) == 1
    r = Requirement(extra[0])
    assert r.name == "myopenfund-open-data-store" and r.extras == {"writer"}
    _assert_floor_and_cap(r)


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
        _assert_floor_and_cap(r)


def _pins(path: Path) -> dict[str, str]:
    """name -> version of every `myopenfund-*==V` line of a requirements file."""
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line.startswith("myopenfund-"):
            r = Requirement(line)
            assert [s.operator for s in r.specifier] == ["=="], line
            pins[r.name] = str(Version(next(iter(r.specifier)).version))
    return pins


def test_requirements_files_pin_siblings_exactly():
    store_file = REPO / "requirements-store.txt"
    dev_file = REPO / "requirements-dev.txt"
    store, dev = _pins(store_file), _pins(dev_file)
    # unit/store jobs use exact pins; each must satisfy the range pyproject publishes
    extra = Requirement(_pyproject()["project"]["optional-dependencies"]["store"][0])
    assert set(store) == {extra.name}
    assert extra.specifier.contains(store[extra.name])
    # vault-contract is not a dependency of the package: only a valid exact pin
    assert set(dev) == {"myopenfund-vault-contract"}
    text = store_file.read_text(encoding="utf-8") + dev_file.read_text(encoding="utf-8")
    assert "git+" not in text
    assert any(
        line.startswith("pyyaml") for line in dev_file.read_text(encoding="utf-8").splitlines()
    )


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
        "${{ (github.event_name == 'workflow_dispatch' && inputs.target == 'testpypi')"
        " && 'testpypi' || 'pypi' }}"
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
    checkout = [s for s in steps if s.get("uses", "").startswith("actions/checkout@")]
    assert checkout[0]["with"]["fetch-depth"] == 0
    upload = _upload_index(steps)
    assert upload and checks[0] < upload[0]


def test_testpypi_publish_skips_existing_but_pypi_does_not():
    steps = {s["name"]: s for s in _workflow(RELEASE)["jobs"]["publish"]["steps"] if "name" in s}
    assert steps["Publish to TestPyPI"]["with"]["skip-existing"] is True
    assert "skip-existing" not in steps["Publish to PyPI"].get("with", {})


def test_concurrency_group():
    assert _workflow(RELEASE)["concurrency"]["group"] == "release-${{ github.ref }}"


def test_dispatch_inputs():
    inputs = _workflow(RELEASE)["on"]["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["target"]
    assert inputs["target"]["type"] == "choice"
    assert inputs["target"]["options"] == ["pypi", "testpypi"]
    assert inputs["target"]["default"] == "pypi"
    assert inputs["target"]["required"] is True


def test_build_dispatch_runs_on_default_branch_only():
    steps = _workflow(RELEASE)["jobs"]["build"]["steps"]
    guard = [s for s in steps if "refs/heads/main" in s.get("run", "")]
    assert len(guard) == 1
    assert guard[0]["if"] == "github.event_name == 'workflow_dispatch'"
    assert "exit 1" in guard[0]["run"]
    assert steps.index(guard[0]) == 0


def test_build_computes_tag_and_refuses_an_existing_pypi_tag():
    job = _workflow(RELEASE)["jobs"]["build"]
    step = [s for s in job["steps"] if s.get("id") == "tag"]
    assert len(step) == 1
    run = step[0]["run"]
    assert step[0]["if"] == "github.event_name == 'workflow_dispatch'"
    assert "importlib.metadata" in run and DIST in run
    assert 'tag="v$version"' in run
    assert 'git ls-remote --tags origin "refs/tags/$tag"' in run
    assert '"$INPUT_TARGET" = "pypi"' in run
    assert "already exists" in run and "exit 1" in run
    upload = _upload_index(job["steps"])
    assert job["steps"].index(step[0]) < upload[0]
    assert job["outputs"]["tag"] == "${{ steps.tag.outputs.tag || github.ref_name }}"
    assert job["outputs"]["sha"] == "${{ github.sha }}"


def test_testpypi_only_for_testpypi_target():
    steps = {s["name"]: s for s in _workflow(RELEASE)["jobs"]["publish"]["steps"] if "name" in s}
    assert "inputs.target == 'testpypi'" in steps["Publish to TestPyPI"]["if"]
    assert "inputs.target == 'testpypi'" in steps["Publish to PyPI"]["if"]
    assert steps["Publish to PyPI"]["if"].startswith("${{ !(")


def _release_job():
    job = _workflow(RELEASE)["jobs"]["github-release"]
    assert job["if"] == (
        "github.event_name == 'push' || "
        "(github.event_name == 'workflow_dispatch' && inputs.target == 'pypi')"
    )
    assert job["permissions"] == {"contents": "write"}
    assert set(job["needs"]) == {"build", "publish"}
    return job


def _assert_tag_created_and_pushed(run):
    # the publication gate forbids a personal address in tracked files: the tagger is the actor
    assert 'user.name="Marc Jeulin"' in run
    assert 'user.email="${GITHUB_ACTOR_ID}+${GITHUB_ACTOR}@users.noreply.github.com"' in run
    assert 'tag -a "$TAG"' in run and '"$SHA"' in run
    assert 'git push origin "refs/tags/$TAG"' in run


def test_github_release_creates_and_pushes_the_tag_on_dispatch_not_testpypi():
    job = _release_job()
    checkout = [s for s in job["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert len(checkout) == 1
    assert checkout[0]["if"] == "github.event_name == 'workflow_dispatch'"
    assert checkout[0]["with"]["persist-credentials"] is True
    assert checkout[0]["with"]["ref"] == "${{ needs.build.outputs.sha }}"
    tag = [s for s in job["steps"] if "git push" in s.get("run", "")]
    assert len(tag) == 1
    assert tag[0]["if"] == "github.event_name == 'workflow_dispatch'"
    _assert_tag_created_and_pushed(tag[0]["run"])


def test_dispatch_tag_step_is_idempotent():
    run = next(s for s in _release_job()["steps"] if "git push" in s.get("run", ""))["run"]
    lookup = run.index('git ls-remote origin "refs/tags/$TAG" "refs/tags/$TAG^{}"')
    assert lookup < run.index("git push")
    assert '"$remote_sha" = "$SHA"' in run  # already there at the built commit: skip
    assert "already exists" in run and "exit 1" in run  # there, but elsewhere: fail
    assert '[ -z "$remote_sha" ]' in run  # absent: create


def test_github_release_is_created_or_completed_on_both_paths():
    job = _release_job()
    release = [s for s in job["steps"] if "gh release create" in s.get("run", "")]
    assert len(release) == 1
    assert "if" not in release[0]
    run = release[0]["run"]
    assert 'gh release view "$TAG"' in run
    assert 'gh release upload "$TAG" dist/* --repo "$GITHUB_REPOSITORY" --clobber' in run
    assert '--target "$SHA"' in run and '--title "$TAG"' in run
    assert "--verify-tag" in run
    assert "--generate-notes" in run




def test_smoke_checks_cli_version_and_reads_packaged_data():
    for path in (RELEASE, TESTS):
        text = path.read_text(encoding="utf-8")
        assert '/bin/company-corpus" --version' in text, path
        assert "company_corpus.concepts" in text, path
        assert "importlib.resources" in text, path
        assert 'dist/*.whl)[store]"' in text, path
        assert "from open_data_store.writer import write_batch" in text, path


def test_tests_workflow_keeps_its_jobs_adds_package_and_runs_weekly():
    workflow = _workflow(TESTS)
    assert {"unit", "store", "package"} <= set(workflow["jobs"])
    (entry,) = workflow["on"]["schedule"]
    minute, hour, day_of_month, month, day_of_week = entry["cron"].split()
    assert day_of_month == "*" and month == "*"
    assert day_of_week.isdigit() and 0 <= int(day_of_week) <= 7  # one weekday: weekly
    runs = " ".join(s.get("run", "") for s in workflow["jobs"]["package"]["steps"])
    assert "build==1.6.1 twine==7.0.0" in runs
    assert "twine check --strict" in runs


def test_schedule_comment_says_what_the_weekly_run_covers():
    text = TESTS.read_text(encoding="utf-8")
    comment = text.split("schedule:\n", 1)[1].split("- cron", 1)[0]
    assert "exact pins" in comment  # the unit and store jobs
    assert "package" in comment and "[store]" in comment and "imports the writer" in comment
    assert "resolve from PyPI within" not in comment  # that was only true of the package job


def test_how_to_release_names_the_version_source():
    text = (REPO / "README.md").read_text(encoding="utf-8")
    section = text.split("**How to release**", 1)[1].split("## Quick start", 1)[0]
    assert "`__version__` in `company_corpus/__init__.py`" in section
    assert "and `version`" not in section
    assert "pip install -e ." in section and "sibling" in section


def test_optional_extra_hints_name_the_published_distribution():
    from company_corpus.eu import arelle_esef
    from company_corpus.registers import ch_ixbrl, financials

    for module in (arelle_esef, ch_ixbrl, financials):
        text = Path(module.__file__).read_text(encoding="utf-8")
        assert f"pip install '{DIST}[eu-financials]'" in text, module
        assert "'.[eu-financials]'" not in text, module
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "'.[be]'" not in readme and f'"{DIST}[be]"' in readme


# ---- fail-closed approval gate (release hardening) -------------------------


def _gate_job():
    job = _workflow(RELEASE)["jobs"]["build"]
    gate = [i for i, s in enumerate(job["steps"]) if s.get("name") == "Check the approval gate"]
    assert len(gate) == 1
    return job, gate[0]


def test_release_checks_the_approval_gate_before_building():
    job, gate = _gate_job()
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    step = job["steps"][gate]
    run = step["run"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "inputs.target == 'testpypi'" in step["env"]["ENV_NAME"]
    assert "'testpypi'" in step["env"]["ENV_NAME"] and "'pypi'" in step["env"]["ENV_NAME"]
    assert 'gh api "repos/$GITHUB_REPOSITORY/environments/$ENV_NAME"' in run
    assert "if ! " in run  # an API failure (non-200) is tested, not ignored
    assert "required_reviewers" in run and "exit 1" in run
    for i, other in enumerate(job["steps"]):
        uses = other.get("uses", "")
        building = uses.startswith(
            ("actions/checkout@", "actions/setup-python@", "actions/upload-artifact@")
        ) or "build" in other.get("run", "")
        if i != gate and (building or other.get("name", "").startswith("Build")):
            assert gate < i, other


def _run_gate(tmp_path, response, status):
    """Run the gate step against a stub `gh` that prints `response` and exits `status`."""
    import os
    import shutil
    import subprocess

    import pytest

    if shutil.which("jq") is None or shutil.which("bash") is None:
        pytest.skip("needs jq and bash")
    job, gate = _gate_job()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "gh"
    stub.write_text(f"#!/bin/sh\ncat <<'JSON'\n{response}\nJSON\nexit {status}\n")
    stub.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_REPOSITORY": "MyOpenFund/example",
        "ENV_NAME": "pypi",
    }
    return subprocess.run(
        ["bash", "-e", "-c", job["steps"][gate]["run"]],
        env=env, capture_output=True, text=True, timeout=30, check=False,
    )


def test_gate_passes_with_a_required_reviewer(tmp_path):
    rules = (
        '{"protection_rules": [{"type": "wait_timer"}, {"type": "required_reviewers",'
        ' "reviewers": [{"type": "User", "reviewer": {"login": "release-approver"}}]}]}'
    )
    assert _run_gate(tmp_path, rules, 0).returncode == 0


def test_gate_fails_without_a_required_reviewer(tmp_path):
    for rules in (
        '{"protection_rules": []}',
        '{"protection_rules": [{"type": "required_reviewers", "reviewers": []}]}',
        '{"protection_rules": [{"type": "branch_policy"}]}',
        "{}",
    ):
        result = _run_gate(tmp_path, rules, 0)
        assert result.returncode == 1, rules
        assert "required reviewer" in result.stdout + result.stderr


def test_gate_fails_closed_when_the_api_call_fails(tmp_path):
    result = _run_gate(tmp_path, "gh: Not Found (HTTP 404)", 1)
    assert result.returncode == 1
    assert "HTTP 404" in result.stdout + result.stderr


# ---- live mode (no store) ---------------------------------------------------


def test_store_job_runs_the_live_tests_too():
    """tests/live importorskips open-data-store (issuer tables): only the store job installs it."""
    runs = [s.get("run", "") for s in _workflow(TESTS)["jobs"]["store"]["steps"]]
    assert "pytest -q tests/opendata tests/live" in runs


def test_package_job_smoke_installs_the_live_extra():
    runs = "\n".join(s.get("run", "") for s in _workflow(TESTS)["jobs"]["package"]["steps"])
    assert 'dist/*.whl)[live]"' in runs
    assert "import company_corpus.live.documents" in runs
    assert "import company_corpus.live.issuer" in runs
    assert "_html_converter()" in runs  # MarkItDown really installed by the extra
    assert 'contracts_api(needed_by="smoke", extra="live")' in runs


def test_dev_requirements_carry_markitdown_in_the_live_range():
    live = [Requirement(d) for d in _pyproject()["project"]["optional-dependencies"]["live"]]
    (markitdown,) = [r for r in live if r.name == "markitdown"]
    dev = [Requirement(line.split("#")[0].strip())
           for line in (REPO / "requirements-dev.txt").read_text(encoding="utf-8").splitlines()
           if line.split("#")[0].strip().startswith("markitdown")]
    assert [r.specifier for r in dev] == [markitdown.specifier]


def test_live_install_hints_name_the_published_distribution():
    from company_corpus.live import documents, issuer

    hint = f"pip install '{DIST}[live]'"
    assert hint in Path(documents.__file__).read_text(encoding="utf-8")
    assert 'extra="live"' in Path(issuer.__file__).read_text(encoding="utf-8")
