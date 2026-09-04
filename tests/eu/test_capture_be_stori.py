"""The BE STORI capture script can't be run here (the WAF blocks our egress IP), but
its HTML extractors are unit-tested against the real archived STORI search form so
the script is known-correct before Marc runs it from a non-blocked network."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_FIX = _ROOT / "tests" / "fixtures" / "eu" / "be_stori_search_2021.html"


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "capture_be_stori", _ROOT / "scripts" / "be" / "capture_be_stori.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fix_dir_points_at_tests_fixtures_eu():
    cap = _load_script()
    expected = Path(cap.__file__).resolve().parents[2] / "tests" / "fixtures" / "eu"
    assert cap.FIX == expected
    assert cap.FIX.is_dir()


def test_extractors_find_stori_form_fields():
    cap = _load_script()
    html = _FIX.read_text(encoding="utf-8")
    names = cap._named_inputs(html)
    hidden = cap._hidden_fields(html)

    # ASP.NET WebForms hidden state
    assert "__VIEWSTATE" in hidden and "__EVENTVALIDATION" in hidden
    # The form action points at Search.aspx
    assert "Search.aspx" in cap._form_action(html)
    # The identity fields the backend will POST
    assert cap._find(names, "company", "text") == "ctl00$ContentPlaceHolder1$CompanyNameTextBox"
    assert cap._find(names, "isin") == "ctl00$ContentPlaceHolder1$isinCodeTextBox"
