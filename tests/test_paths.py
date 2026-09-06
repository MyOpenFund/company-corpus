"""Task 7 — every third-party identifier is validated before it becomes a path.

Two families of failure are covered here:

* a value from outside (an OAM-supplied filename, a register id from a spec
  file) used verbatim as a path component, so ``..`` escaped the corpus and an
  absolute path wrote outside ``data_dir`` entirely (Rob-C7 / DI-M4);
* a value that was *repaired* into something plausible instead of rejected --
  ``"N/A"`` digit-stripped and zero-padded into ``"0000000000"``, a lower-case
  LEI kept as a second spelling of one issuer (DI-I4 / DI-I7).
"""
from __future__ import annotations

import pytest

from company_corpus.config import Config, normalize_cik, normalize_lei
from company_corpus.paths import UnsafeIdentifier, safe_component, safe_filename
from company_corpus.registers.identity import (
    _norm_ch_number,
    _norm_cvr,
    _norm_ico,
    _norm_kbo,
    _norm_orgnr,
    _norm_rcs,
    _norm_registrikood,
    _norm_ytunnus,
    resolve_register_specs,
)
from company_corpus.storage import Storage

# ---------------------------------------------------------------------------
# safe_component / safe_filename
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", ".", "..", "../../escaped",
                                   "/abs/path", "a\\b", "a\x00b", "x" * 200])
def test_unsafe_components_are_refused(value):
    with pytest.raises(UnsafeIdentifier):
        safe_component(value, max_length=128)


def test_safe_components_pass_through():
    assert safe_component(" B60814 ", max_length=128) == "B60814"


@pytest.mark.parametrize("value", [
    "..\\..\\windows",          # Windows-style traversal
    "C:\\Windows\\system32",    # Windows absolute path
    "dir/sub",                  # a nested path where one component is expected
    "\x00",
])
def test_windows_and_nested_shapes_are_refused(value):
    with pytest.raises(UnsafeIdentifier):
        safe_component(value, max_length=128)


def test_hostile_filename_falls_back_to_a_deterministic_hash():
    a = safe_filename("../../../pwn.bin", url="https://x.invalid/f", max_length=128)
    b = safe_filename("../../../pwn.bin", url="https://x.invalid/f", max_length=128)
    assert a == b and "/" not in a and a.endswith(".bin")


def test_hostile_filenames_with_different_urls_do_not_collide():
    a = safe_filename("../../a.bin", url="https://x.invalid/1", max_length=128)
    b = safe_filename("../../a.bin", url="https://x.invalid/2", max_length=128)
    assert a != b


def test_absolute_filename_falls_back_and_keeps_no_separator():
    name = safe_filename("/etc/passwd", url="https://x.invalid/f", max_length=128)
    assert "/" not in name and "\\" not in name and name


# ---------------------------------------------------------------------------
# The writers
# ---------------------------------------------------------------------------


def test_register_table_write_refuses_an_escaping_ident(config):
    with pytest.raises(UnsafeIdentifier):
        Storage(config).write_register_financials_table("../../escaped", [{"a": 1}])
    assert not (config.data_dir.parent / "escaped.jsonl").exists()


def test_register_table_write_refuses_an_absolute_ident(config, tmp_path):
    with pytest.raises(UnsafeIdentifier):
        Storage(config).write_register_financials_table(f"{tmp_path}/pwn", [{"a": 1}])
    assert not (tmp_path / "pwn.jsonl").exists()


def test_eu_writer_normalises_the_lei(config):
    st = Storage(config)
    st.write_eu_financials_table(" 529900t8bm49aurskb52 ", [{"period_end": "2024-12-31"}])
    assert (config.financials_eu_dir / "529900T8BM49AURSKB52.jsonl").exists()
    with pytest.raises(ValueError):
        st.write_eu_financials_table("TOOSHORT", [{"a": 1}])


def test_eu_writer_merges_the_two_spellings_of_one_lei(config):
    """One issuer, one file: the case-folded spelling must not open a second."""
    st = Storage(config)
    st.write_eu_financials_table("529900T8BM49AURSKB52",
                                 [{"period_end": "2023-12-31", "concept": "revenue"}])
    st.write_eu_financials_table("529900t8bm49aurskb52",
                                 [{"period_end": "2024-12-31", "concept": "revenue"}])
    written = sorted(p.name for p in config.financials_eu_dir.glob("*.jsonl"))
    assert written == ["529900T8BM49AURSKB52.jsonl"]
    rows = (config.financials_eu_dir / "529900T8BM49AURSKB52.jsonl").read_text().splitlines()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# normalize_lei / normalize_cik
# ---------------------------------------------------------------------------


def test_normalize_lei_upper_cases_and_strips():
    assert normalize_lei("  529900t8bm49aurskb52\n") == "529900T8BM49AURSKB52"


@pytest.mark.parametrize("bad", ["", "   ", "TOOSHORT", "529900T8BM49AURSKB5",
                                 "529900T8BM49AURSKB521", "529900T8BM49AURSKB5-",
                                 "../../pwn", "N/A", None])
def test_normalize_lei_refuses_a_malformed_lei(bad):
    with pytest.raises(ValueError):
        normalize_lei(bad)


def test_normalize_lei_validates_the_shape_not_the_check_digits():
    """Deliberate boundary: ISO 17442 shape only, no ISO 7064 mod-97-10 test.

    ``…KB53`` is ``529900T8BM49AURSKB52`` with a wrong check pair; it is still a
    structurally valid path component and is accepted. Existence and checksum are
    GLEIF's authority, not the writer's -- the writer's job is one canonical
    spelling per issuer. Enforcing the checksum here would also reject the
    well-formed synthetic LEIs the fixtures are built from.
    """
    assert normalize_lei("529900t8bm49aurskb53") == "529900T8BM49AURSKB53"


@pytest.mark.parametrize("bad", ["0", "0000000000", "00000000", " 0 ", "CIK0000000000"])
def test_normalize_cik_refuses_an_all_zero_cik(bad):
    with pytest.raises(ValueError):
        normalize_cik(bad)


def test_normalize_cik_still_accepts_a_real_cik():
    assert normalize_cik(320193) == "0000320193"
    assert normalize_cik("CIK0000320193") == "0000320193"


# ---------------------------------------------------------------------------
# Register identifier validation (DI-I4): no guessing, ever
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("junk", ["N/A", "n/a", "", "  ", "Handelsregister B",
                                  "—", 0, "0", "../../pwn"])
def test_junk_identifiers_resolve_to_unresolved(junk, make_fetcher):
    rows = resolve_register_specs([{"ico": junk}], fetcher=make_fetcher({}))
    assert rows[0]["status"] == "unresolved"
    assert not rows[0].get("ico")


@pytest.mark.parametrize("junk", ["N/A", "", "  ", "—", "../../pwn", "0", 0])
def test_junk_identifiers_are_unresolved_on_every_direct_path(junk, make_fetcher):
    for key in ("ch_number", "orgnr", "be_number", "business_id", "rcs", "cvr",
                "registrikood", "ico"):
        rows = resolve_register_specs([{key: junk}], fetcher=make_fetcher({}))
        assert rows[0]["status"] == "unresolved", (key, junk)
        assert not rows[0].get(key), (key, junk)


def test_no_norm_helper_ever_returns_an_all_zero_id():
    for helper in (_norm_kbo, _norm_registrikood, _norm_ico, _norm_cvr):
        assert helper("N/A") is None


@pytest.mark.parametrize("zeros", ["0", "00000000", "0000000000", "0000000-0"])
def test_no_norm_helper_ever_accepts_an_all_zero_id(zeros):
    for helper in (_norm_kbo, _norm_registrikood, _norm_ico, _norm_cvr,
                   _norm_orgnr, _norm_ytunnus, _norm_ch_number, _norm_rcs):
        assert helper(zeros) is None, (helper.__name__, zeros)


def test_valid_identifiers_still_normalise():
    assert _norm_kbo("0403.227.515") == "0403227515"
    assert _norm_kbo("403227515") == "0403227515"     # 9-digit legacy form
    assert _norm_ico("31 322 832") == "31322832"
    assert _norm_rcs("B 60814") == "B60814"
    assert _norm_orgnr("NO 923 609 016") == "923609016"
    assert _norm_ytunnus(" 2919415-2 ") == "2919415-2"
    assert _norm_cvr("  04256790  ") == "04256790"
    assert _norm_registrikood("EE11098261") == "11098261"
    assert _norm_ch_number("510976") == "00510976"
    assert _norm_ch_number(" oc372294 ") == "OC372294"


@pytest.mark.parametrize("helper,bad", [
    (_norm_kbo, "12345678"),          # 8 digits: neither the 9- nor 10-digit form
    (_norm_ico, "313228321"),         # 9 digits, not 8
    (_norm_registrikood, "1109826"),  # 7 digits, not 8 -- padding would invent one
    (_norm_cvr, "242567901"),
    (_norm_orgnr, "92360901"),
    (_norm_ytunnus, "29194152"),      # missing the check-digit separator
    (_norm_rcs, "60814"),             # no register letter
    (_norm_ch_number, "SC74102"),     # 7 characters, not 8
])
def test_wrong_length_identifiers_are_rejected_not_padded(helper, bad):
    assert helper(bad) is None


def test_a_config_knob_bounds_the_component_length():
    assert Config().max_path_component_length == 128


# ---------------------------------------------------------------------------
# The producers: an unusable identifier is a visible skip, never a lost batch
# ---------------------------------------------------------------------------


def test_emit_entity_rows_refuses_an_escaping_entity_id(config):
    from company_corpus.registers._common import _emit_entity_rows, _make_out

    out, coverage = _make_out(), []
    _emit_entity_rows("../../pwn", [{"concept": "revenue", "value": 1}], 1,
                      {"rcs": "../../pwn", "lei": None}, Storage(config), out,
                      coverage, write=True)
    assert coverage[0]["status"] == "invalid-identifier" and coverage[0]["error"]
    assert out["errors"] == 1 and out["with_financials"] == 0 and out["paths"] == []
    assert not (config.data_dir.parent / "pwn.jsonl").exists()


def test_emit_entity_rows_refuses_the_same_id_in_a_dry_run(config):
    """A dry-run must report the refusal a real run would, not a green 'ok'."""
    from company_corpus.registers._common import _emit_entity_rows, _make_out

    out, coverage = _make_out(), []
    _emit_entity_rows("../../pwn", [{"concept": "revenue", "value": 1}], 1,
                      {"rcs": "../../pwn", "lei": None}, Storage(config), out,
                      coverage, write=False)
    assert coverage[0]["status"] == "invalid-identifier"
    assert out["errors"] == 1 and out["with_financials"] == 0


def test_emit_entity_rows_refuses_a_none_entity_id(config):
    """``str(None)`` is the perfectly usable component ``"None"`` -- every
    unreadable entity in a run would have shared one ``None.jsonl``."""
    from company_corpus.registers._common import _emit_entity_rows, _make_out

    out, coverage = _make_out(), []
    _emit_entity_rows(None, [{"concept": "revenue", "value": 1}], 1,
                      {"ico": None, "lei": None}, Storage(config), out,
                      coverage, write=True)
    assert coverage[0]["status"] == "invalid-identifier"
    assert not (config.financials_register_dir / "None.jsonl").exists()


def test_lu_declarer_with_a_hostile_rcs_is_recorded_not_written(tmp_path):
    """The declarer's <RcsNumber> reached the writer without _norm_rcs (DI-I4)."""
    import json

    from company_corpus.registers.financials import build_lu_financials_from_files

    src = tmp_path / "bulk.xml"
    src.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<STATECCDBDeclarations><Declarer>"
        "<RcsNumber>../../pwn</RcsNumber>"
        "<LegalUnitName>Pwn S.A.</LegalUnitName>"
        "</Declarer></STATECCDBDeclarations>", encoding="utf-8")
    cfg = Config(data_dir=tmp_path / "data", contact="t@e.com")
    out = build_lu_financials_from_files([src], config=cfg, write=True)
    assert out["entities"] == 1 and out["errors"] == 1 and out["with_financials"] == 0
    cov = [json.loads(x) for x in
           (cfg.reports_dir / "register_coverage_lbr.jsonl").read_text().splitlines()]
    assert cov[0]["status"] == "invalid-identifier" and cov[0]["rcs"] == "../../pwn"
    assert not (tmp_path / "pwn.jsonl").exists()
    assert not cfg.financials_register_dir.exists()


def test_ch_bulk_skips_a_member_whose_number_is_unusable(tmp_path):
    """_norm_ch_number returns None now; a None must never travel to the writer."""
    import zipfile

    from company_corpus.registers.ch_bulk import iter_ch_bulk

    zip_path = tmp_path / "bulk.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Prod223_4212_02855129_20260331.html", b"<html>ok</html>")
        zf.writestr("Prod223_4212_N/A_20260331.html", b"<html>junk</html>")
        zf.writestr("Prod223_4212_00000000_20260331.html", b"<html>zeros</html>")
    numbers = [n for n, _ in iter_ch_bulk(str(zip_path))]
    assert numbers == ["02855129"]
