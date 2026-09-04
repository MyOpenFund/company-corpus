"""Register-financials producer — Brreg JSON accounts + CH iXBRL -> the curated schema.

Two producers share a common tail:
- ``build_register_financials``: Norwegian Brreg register (multi-period per entity).
- ``build_ch_financials``: UK Companies House Accounts Bulk Data zip (one period per entity).

Both use the shared ``_emit_entity_rows`` helper for writing the financials table and
updating coverage/counter state, so the storage + coverage logic is only written once.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from ..config import Config
from ..financials import rows_from_base
from ..storage import Storage
from ..xbrl import flatten_oim_json
from ._common import (
    _base,
    _emit_entity_rows,
    _emit_mapped,
    _finalise_coverage,
    _lei_or_none,
    _make_out,
    _record_source_error,
    _summary,
)
from .bnb_cbso import fetch_bnb_deposit as _fetch_bnb_deposit
from .bnb_xbrl import open_bnb_deposit, parse_bnb_document
from .ch_bulk import iter_ch_bulk
from .ch_ixbrl import oim_from_ch_html
from .concepts_be import map_bnb_facts
from .concepts_ee import map_ee_report as _map_ee_report
from .concepts_fi import map_fi_facts
from .concepts_lu import map_lu_entity
from .concepts_no import map_brreg_entry
from .concepts_sk import map_sk_vykaz as _map_sk_vykaz
from .concepts_uk import map_ch_facts
from .ee_csv import download_ee_bulk as _download_ee_bulk, iter_ee_reports as _iter_ee_reports
from .fi_prh_xbrl import parse_fi_facts
from .identity import resolve_register_specs
from .lu_cdb import iter_lu_declarers
from .no_brreg import fetch_brreg_accounts
from .prh_api import fetch_fi_financial, list_fi_dates
from .sk_registeruz import (
    fetch_entity as _sk_fetch_entity,
    fetch_sablona as _sk_fetch_sablona,
    fetch_vykaz as _sk_fetch_vykaz,
    fetch_zavierka as _sk_fetch_zavierka,
)

# Y-tunnus pattern: NNNNNNN-N (7 digits, hyphen, 1 check digit)
_YTUNNUS_RE = re.compile(r"(\d{7}-\d)")

# Leverage basis (C1): whether a register's total_debt / debt_to_equity / net_debt
# / debt_to_assets are built from real financial *borrowings* or from *total
# liabilities* (a gearing proxy). Stamped onto those four derived rows so a
# leverage screen can tell true debt from a payables-inflated proxy.
_BASIS_BORROWINGS = "borrowings"
_BASIS_TOTAL_LIABILITIES = "total_liabilities"


def _dedupe_latest(entries: list[dict]) -> list[dict]:
    """Collapse raw Brreg entries so each (regnskapsperiode.tilDato, regnskapstype)
    appears once, keeping the highest submission `id` — Brreg can return corrected /
    resubmitted accounts for the same period, which would otherwise double-count. When
    an `id` is missing on either side, the last-seen entry for that key wins. Operates
    on RAW entries (which still carry `id`), so `map_brreg_entry` stays unchanged."""
    best: dict[tuple, dict] = {}
    for e in entries:
        key = ((e.get("regnskapsperiode") or {}).get("tilDato"), e.get("regnskapstype"))
        cur = best.get(key)
        if cur is None:
            best[key] = e
            continue
        e_id, cur_id = e.get("id"), cur.get("id")
        # Type-safe: a heterogeneous / non-int `id` would make `e_id >= cur_id` raise
        # TypeError and abort the batch; treat any non-int id as "keep last-seen".
        if not isinstance(e_id, int) or not isinstance(cur_id, int) or e_id >= cur_id:
            best[key] = e
    return list(best.values())


def build_register_financials(specs, *, fetcher, config: Config, write: bool = True) -> dict:
    resolved = resolve_register_specs(specs, fetcher=fetcher)
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()
    for r in resolved:
        out["entities"] += 1
        if not r.get("orgnr"):
            coverage.append({"orgnr": None, "lei": r.get("lei"), "status": "unresolved"})
            out["no_financials"] += 1
            continue
        try:  # one malformed record must not abort the whole batch (nor the coverage write)
            rows: list[dict] = []
            n = 0
            had_unbalanced = False
            for entry in _dedupe_latest(fetch_brreg_accounts(r["orgnr"], fetcher=fetcher)):
                mapped = map_brreg_entry(entry)
                if not mapped:
                    continue
                # R-I1: balance gate — skip this period if assets != equity+liabilities.
                if mapped["unbalanced"]:
                    had_unbalanced = True
                    continue
                s = _summary(mapped, r.get("name") or r["orgnr"])
                rows.extend(rows_from_base(
                    _base(r["orgnr"], r.get("lei"), mapped, s,
                          country="NO", source="brreg"), s))
                n += 1
            cov_base = {"orgnr": r["orgnr"], "lei": r.get("lei")}
            # If every period was rejected by the balance gate, classify as unbalanced.
            if not rows and had_unbalanced:
                coverage.append({**cov_base, "status": "unbalanced"})
                out["unbalanced"] += 1
                continue
            # NO/NGAAP gives total liabilities, not pure borrowings -> gearing proxy.
            _emit_entity_rows(r["orgnr"], rows, n, cov_base, storage, out, coverage,
                              write=write, leverage_basis=_BASIS_TOTAL_LIABILITIES)
        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            cov_base = {"orgnr": r["orgnr"], "lei": r.get("lei")}
            _record_source_error(exc, cov_base, entity_id=r["orgnr"],
                                 source="brreg", out=out, coverage=coverage)
            continue
    return _finalise_coverage(out, coverage, config, "brreg", write=write)


def build_ch_financials(
    zip_path: str,
    *,
    config: Config,
    write: bool = True,
    limit: int | None = None,
    cntlr=None,
) -> dict:
    """Parse a Companies House Accounts Bulk Data zip and emit the curated schema.

    Iterates ``iter_ch_bulk(zip_path, limit=limit)``, writes each HTML to a temp
    file, parses it with Arelle via ``oim_from_ch_html``, flattens + maps through
    ``map_ch_facts``, and emits rows via the shared tail.

    A single Arelle ``Cntlr`` is shared for the whole batch (first file ~14 s
    taxonomy download, subsequent files ~0.7 s each).  Pass a pre-built ``cntlr``
    to skip the Arelle dependency check (useful for unit tests).

    Parameters
    ----------
    zip_path:  Path to a CH Accounts Bulk Data .zip file.
    config:    Config instance (data_dir for output).
    write:     Persist rows + coverage (default True); False for dry-run.
    limit:     Cap on entities processed.
    cntlr:     Optional shared Arelle Cntlr; created internally if None.
    """
    own_cntlr = cntlr is None
    if own_cntlr:
        try:
            from arelle import Cntlr as _ArelleCntlr
        except ImportError as exc:
            raise ImportError(
                "UK Companies House iXBRL parsing needs Arelle — install the optional "
                "extra: pip install '.[eu-financials]'"
            ) from exc
        cntlr = _ArelleCntlr.Cntlr(logFileName="logToBuffer")

    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    try:
        for ch_number, html_bytes in iter_ch_bulk(zip_path, limit=limit):
            out["entities"] += 1
            cov_base: dict = {"ch_number": ch_number, "lei": None}

            try:
                # Arelle needs a real file path (not bytes). Create a named temp
                # file, then write + parse inside a single try/finally so the file
                # is always removed — even if the write itself raises.
                tmp_fd, tmp_name = tempfile.mkstemp(suffix=".html")
                try:
                    os.close(tmp_fd)
                    Path(tmp_name).write_bytes(html_bytes)
                    oim = oim_from_ch_html(tmp_name, cntlr=cntlr)
                finally:
                    Path(tmp_name).unlink(missing_ok=True)

                flat = flatten_oim_json(
                    oim, filed="", form="accounts", accn=f"ch-{ch_number}")
                mapped = map_ch_facts(flat)

                # C1: None check first, then unbalanced, then empty values — so that
                # the unbalanced branch is reached before the no-values branch (an
                # unbalanced filing returns values={}, which would otherwise fall
                # through to the no-financials path first).
                if mapped is None:
                    coverage.append({**cov_base, "status": "no-financials"})
                    out["no_financials"] += 1
                    continue

                # UK short_term_debt mirrors current liabilities so total_debt
                # reconstructs total liabilities -> a gearing proxy, not borrowings.
                # (accession prefix "ch" differs from source "companies_house".)
                _emit_mapped(
                    mapped, ch_number, None, None, cov_base,
                    country="GB", source="companies_house", form="ch",
                    leverage_basis=_BASIS_TOTAL_LIABILITIES,
                    storage=storage, out=out, coverage=coverage, write=write,
                )

            except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch
                _record_source_error(exc, cov_base, entity_id=ch_number,
                                     source="companies_house", out=out, coverage=coverage)
                continue

    finally:
        if own_cntlr:
            cntlr.close()

    return _finalise_coverage(out, coverage, config, "companies_house", write=write)


# ---------------------------------------------------------------------------
# Belgium BNB CBSO register producer
# ---------------------------------------------------------------------------
# Note: consolidated model detection (m120) is out of scope; all entities are
# emitted with basis="company" (the statutory individual accounts model).

def _be_pipeline(
    xbrl_source,
    entity_id: str,
    lei,
    name: str,
    *,
    storage: Storage,
    out: dict,
    coverage: list[dict],
    write: bool,
) -> None:
    """Parse, map, and emit rows for one BNB XBRL source (shared by both BE paths).

    ``xbrl_source`` is either a :class:`pathlib.Path` (for the keyless path)
    or ``bytes`` (for the API path after deposit extraction).  Raises on any
    parse or mapping error — callers must wrap in ``try/except``.
    """
    # Single parse for both facts and period_end (M1: avoids double-parsing at batch scale).
    flat, pe = parse_bnb_document(xbrl_source)
    mapped = map_bnb_facts(flat, period_end=pe)

    # BE emits real m51 financial borrowings (x-checked) -> borrowings-based leverage.
    _emit_mapped(
        mapped, entity_id, lei, name, {"be_number": entity_id, "lei": lei},
        country="BE", source="bnb", form="bnb", leverage_basis=_BASIS_BORROWINGS,
        storage=storage, out=out, coverage=coverage, write=write,
    )


def build_be_financials_from_files(
    paths,
    *,
    config: Config,
    write: bool = True,
) -> dict:
    """Parse a list of local BNB .xbrl or deposit .zip files -> the curated schema.

    Each path is either a bare ``-data.xbrl`` file or a BNB deposit ``.zip``
    (the three-file archive; the ``*-data.xbrl`` member is extracted via
    :func:`open_bnb_deposit`).

    The entity identifier (KBO) is derived from the filename: the last
    underscore-delimited token of the stem is used (e.g.
    ``m02_full_0648822310.xbrl`` → ``"0648822310"``).

    Parameters
    ----------
    paths:
        Iterable of file paths (str or Path) pointing to ``.xbrl`` or ``.zip``
        files.
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    """
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    for path in paths:
        out["entities"] += 1
        path_obj = Path(str(path))
        # KBO from the last underscore-delimited stem token (or the whole stem).
        entity_id = path_obj.stem.rsplit("_", 1)[-1]
        cov_base: dict = {"be_number": entity_id, "lei": None}

        try:
            if path_obj.suffix.lower() == ".zip":
                xbrl_bytes = open_bnb_deposit(path_obj.read_bytes())
                xbrl_source = xbrl_bytes   # bytes
            else:
                xbrl_source = path_obj     # Path (parsed from disk)

            _be_pipeline(
                xbrl_source, entity_id, None, entity_id,
                storage=storage, out=out, coverage=coverage, write=write,
            )

        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=entity_id,
                                 source="bnb", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "bnb", write=write)


def build_lu_financials_from_files(
    paths,
    *,
    config: Config,
    write: bool = True,
    rcs_filter=None,
) -> dict:
    """Parse a list of local LBR eCDF XML files and emit the curated schema.

    Each file is a STATEC/LBR eCDF bulk XML (as published on data.public.lu) or
    a single-entity XML; it may contain one or more ``<Declarer>`` elements.
    Each declarer is mapped via :func:`map_lu_entity` and emitted through the
    shared ``_emit_entity_rows`` tail.

    Parameters
    ----------
    paths:
        Iterable of file paths (str or Path) pointing to eCDF XML files.
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    rcs_filter:
        Optional collection of RCS strings (e.g. ``{"B60814"}``).  When
        provided, only declarers whose ``rcs`` is in the set are processed.
        Pass ``None`` (the default) to process every declarer.
    """
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    for path in paths:
        path_obj = Path(str(path))
        # Materialise all declarers for this file inside a try so that a bad
        # path (missing file, malformed XML) is isolated from the rest of the batch.
        try:
            declarers = list(iter_lu_declarers(path_obj, rcs_filter=rcs_filter))
        except Exception as exc:  # noqa: BLE001 — path-level error, no entity_id
            cov_base = {"rcs": path_obj.name, "lei": None}
            _record_source_error(exc, cov_base, entity_id=path_obj.name,
                                 source="lbr", out=out, coverage=coverage)
            continue

        for declarer in declarers:
            out["entities"] += 1
            entity_id = declarer["rcs"]
            cov_base: dict = {"rcs": entity_id, "lei": None}

            try:
                mapped = map_lu_entity(declarer["declarations"])

                # LU maps real borrowings (ecdf financial-debt lines) -> borrowings basis.
                _emit_mapped(
                    mapped, entity_id, None, declarer.get("name"), cov_base,
                    country="LU", source="lbr", form="lbr",
                    leverage_basis=_BASIS_BORROWINGS,
                    storage=storage, out=out, coverage=coverage, write=write,
                )

            except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
                _record_source_error(exc, cov_base, entity_id=entity_id,
                                     source="lbr", out=out, coverage=coverage)
                continue

    return _finalise_coverage(out, coverage, config, "lbr", write=write)


def build_be_financials(
    specs,
    *,
    fetcher,
    config: Config,
    key: str,
    write: bool = True,
) -> dict:
    """Fetch BNB deposit via the CBSO API and emit the curated schema.

    Resolves each spec to a KBO number via :func:`resolve_register_specs`, then
    calls :func:`fetch_bnb_deposit` to retrieve the latest deposit bytes.  The
    deposit may be a deposit ``.zip`` or a bare ``.xbrl`` depending on the model
    type; both are handled transparently.

    Live / scale validation — rate limits, pagination behaviour for entities with
    a large deposit history, key-quota behaviour — is a **maintainer step** and
    is intentionally out of scope for this function.  Unit-test with a stubbed
    fetcher returning fixture bytes.

    Parameters
    ----------
    specs:
        List of spec dicts accepted by :func:`resolve_register_specs` (e.g.
        ``[{"be_number": "0648822310"}]`` or ``[{"lei": "…"}]``).
    fetcher:
        A :class:`company_corpus.http.Fetcher` instance (or any object that
        exposes ``get_json`` and ``get``).
    config:
        Config instance (``data_dir`` for output).
    key:
        CBSO Authentic Data API subscription key.
    write:
        Persist rows + coverage (default True); False for dry-run.
    """
    resolved = resolve_register_specs(specs, fetcher=fetcher)
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    for r in resolved:
        out["entities"] += 1
        be_number = r.get("be_number")
        cov_base: dict = {"be_number": be_number, "lei": r.get("lei")}

        if not be_number:
            coverage.append({**cov_base, "status": "unresolved"})
            out["no_financials"] += 1
            continue

        try:
            deposit_bytes = _fetch_bnb_deposit(be_number, fetcher=fetcher, key=key)
            if deposit_bytes is None:
                coverage.append({**cov_base, "status": "no-financials"})
                out["no_financials"] += 1
                continue

            # Detect deposit zip (PK magic) vs bare .xbrl bytes.
            xbrl_bytes = (
                open_bnb_deposit(deposit_bytes)
                if deposit_bytes[:4] == b"PK\x03\x04"
                else deposit_bytes
            )

            _be_pipeline(
                xbrl_bytes, be_number, r.get("lei"), r.get("name") or be_number,
                storage=storage, out=out, coverage=coverage, write=write,
            )

        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=be_number,
                                 source="bnb", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "bnb", write=write)


# ---------------------------------------------------------------------------
# Finland PRH open-data XBRL register producer
# ---------------------------------------------------------------------------
# Keyless open API — no subscription key required.  The XBRL instance document
# uses a dimensional model (fi_MC metric codes); ``parse_fi_facts`` (stdlib,
# no Arelle) + ``map_fi_facts`` (NO-FALSE-DATA gate) handle parsing/mapping.
# ``basis="company"`` (FAS individual statutory accounts). EUR only.
#
# net_income = x740 (FINAL, after appropriations), NEVER x738.
# Liabilities-based leverage: total liabilities (x513) emitted; the long/short
# maturity split (x583/x816) is suppressed because the label linkbase is not
# included in the instance — no-false-data prevents guessing which is LT vs ST.


def _fi_entity_id(path_obj: Path) -> str:
    """Extract Y-tunnus (NNNNNNN-N) from the filename stem.

    E.g. ``fi_2919415-2_full_2024.xml`` → ``"2919415-2"``.  Falls back to the
    full stem when no match is found.
    """
    m = _YTUNNUS_RE.search(path_obj.stem)
    return m.group(1) if m else path_obj.stem


def _fi_pipeline(
    xbrl_source,
    entity_id: str,
    lei,
    name: str,
    *,
    storage: Storage,
    out: dict,
    coverage: list[dict],
    write: bool,
) -> None:
    """Parse, map, and emit rows for one PRH XBRL source (shared by both FI paths).

    ``xbrl_source`` is a :class:`pathlib.Path` (keyless file path) or ``bytes``
    (API path).  Raises on any parse or mapping error — callers must wrap in
    ``try/except``.
    """
    parsed = parse_fi_facts(xbrl_source)
    mapped = map_fi_facts(parsed)

    # FI suppresses the maturity split, so the engine emits NO leverage rows —
    # there is nothing to stamp, hence no leverage_basis (left None).
    _emit_mapped(
        mapped, entity_id, lei, name, {"business_id": entity_id, "lei": lei},
        country="FI", source="prh", form="prh", leverage_basis=None,
        storage=storage, out=out, coverage=coverage, write=write,
    )


def build_fi_financials_from_files(
    paths,
    *,
    config: Config,
    write: bool = True,
) -> dict:
    """Parse a list of local PRH XBRL ``.xml`` files → the curated schema.

    The entity identifier (Y-tunnus) is extracted from the filename using the
    ``NNNNNNN-N`` pattern (e.g. ``fi_2919415-2_full_2024.xml`` → ``"2919415-2"``).

    Parameters
    ----------
    paths:
        Iterable of file paths (str or Path) pointing to PRH XBRL ``.xml`` files.
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    """
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    for path in paths:
        out["entities"] += 1
        path_obj = Path(str(path))
        entity_id = _fi_entity_id(path_obj)
        cov_base: dict = {"business_id": entity_id, "lei": None}

        try:
            _fi_pipeline(
                path_obj, entity_id, None, entity_id,
                storage=storage, out=out, coverage=coverage, write=write,
            )
        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=entity_id,
                                 source="prh", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "prh", write=write)


def build_fi_financials(
    specs,
    *,
    fetcher,
    config: Config,
    write: bool = True,
) -> dict:
    """Fetch PRH XBRL via the open API and emit the curated schema.

    Resolves each spec to a Y-tunnus via :func:`resolve_register_specs`, then
    calls :func:`list_fi_dates` to find the latest available date and
    :func:`fetch_fi_financial` to retrieve the XBRL bytes.

    Keyless — no API key required.  Live / scale validation — rate limits,
    pagination behaviour, filing completeness for a given date — is a
    **controller step** and is intentionally out of scope for this function.
    Unit-test with a stubbed fetcher returning fixture bytes.

    Parameters
    ----------
    specs:
        List of spec dicts accepted by :func:`resolve_register_specs` (e.g.
        ``[{"business_id": "2919415-2"}]`` or ``[{"lei": "…"}]``).
    fetcher:
        A :class:`company_corpus.http.Fetcher` instance (or any object that
        exposes ``get_json`` and ``get``).
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    """
    resolved = resolve_register_specs(specs, fetcher=fetcher)
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    for r in resolved:
        out["entities"] += 1
        business_id = r.get("business_id")
        cov_base: dict = {"business_id": business_id, "lei": r.get("lei")}

        if not business_id:
            coverage.append({**cov_base, "status": "unresolved"})
            out["no_financials"] += 1
            continue

        try:
            dates = list_fi_dates(business_id, fetcher=fetcher)
            if not dates:
                coverage.append({**cov_base, "status": "no-financials"})
                out["no_financials"] += 1
                continue

            latest_date = max(dates)
            xbrl_bytes = fetch_fi_financial(business_id, latest_date, fetcher=fetcher)
            if xbrl_bytes is None:
                # PRH itself listed a filing for this date, so an empty document
                # is a DEAD SOURCE (fetch failed / returned nothing), never the
                # issuer's own "filed nothing" — see prh_api's WARNING log.
                _record_source_error(
                    f"PRH returned no document for {business_id} @ {latest_date}",
                    cov_base, entity_id=business_id, source="prh",
                    out=out, coverage=coverage)
                continue

            _fi_pipeline(
                xbrl_bytes, business_id, r.get("lei"), r.get("name") or business_id,
                storage=storage, out=out, coverage=coverage, write=write,
            )

        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=business_id,
                                 source="prh", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "prh", write=write)


# ---------------------------------------------------------------------------
# Denmark Erhvervsstyrelsen / Virk register producer
# ---------------------------------------------------------------------------
# Two paths (both keyless via Virk Regnskaber — no API key required):
#
#   Path A (ESEF / IFRS) — listed issuers file bare XBRL tagged with the
#       ifrs-full taxonomy.  ``map_dk_esef`` → ``summaries_from_flat(IFRS_CONCEPTS)``
#       (100 % reuse of the EU IFRS engine) → borrowings-based leverage, same
#       ``debt_to_equity`` formula as the EU pillar.  source="erst-ifrs".
#
#   Path B (FSA / DK-GAAP) — private companies file bare XBRL tagged with
#       the Erhvervsstyrelsen FSA taxonomy (http://xbrl.dcca.dk/fsa).
#       ``parse_fsa_facts`` → ``map_fsa_facts`` → ``_emit_entity_rows``
#       (liabilities-based leverage, NO-FALSE-DATA gate, §32 revenue suppression).
#       source="erst-fsa".
#
# entity_id = CVR (8 digits, from xbrli:identifier scheme dcca.dk/cvr for FSA;
# or LEI from xbrli:identifier scheme iso/17442 for ESEF; or filename fallback).
# Currency: DKK.  basis="company" (statutory individual accounts).

_NS_XBRLI_STR = "http://www.xbrl.org/2003/instance"
_CVR_8DIGIT_RE = re.compile(r"(\d{8})")


def _route_dk_xml(xml_bytes: bytes) -> "str | None":
    """Route DK XML bytes to ``'esef'`` or ``'fsa'`` by sniffing the first 8 KB.

    ``'esef'``: ``ifrs-full`` namespace marker found → listed ESEF XBRL (Path A).
    ``'fsa'``:  ``xbrl.dcca.dk/fsa`` namespace found → DK-GAAP FSA XBRL (Path B).
    ``None``:   unrecognised document — skip.
    """
    header = xml_bytes[:8192]
    if b"ifrs.org" in header and b"ifrs-full" in header:
        return "esef"
    if b"xbrl.dcca.dk/fsa" in header:
        return "fsa"
    return None


def _dk_entity_id_from_xml(xml_bytes: bytes, path_obj: Path, route: str) -> str:
    """Extract entity_id from the XBRL document's ``xbrli:identifier``.

    FSA (Path B): CVR from a scheme URI containing ``dcca`` (e.g.
    ``http://www.dcca.dk/cvr``).  Falls back to the first 8-digit sequence
    in the filename stem.

    ESEF (Path A): LEI from a scheme URI containing ``17442`` (ISO 17442).
    Falls back to the filename stem.

    The first matching identifier in document order wins (all contexts in a
    filing share the same entity identifier).
    """
    import xml.etree.ElementTree as _ET_DK
    ident_tag = f"{{{_NS_XBRLI_STR}}}identifier"
    try:
        root = _ET_DK.fromstring(xml_bytes)
        for ident in root.iter(ident_tag):
            scheme = ident.get("scheme", "")
            val = (ident.text or "").strip()
            if not val:
                continue
            if route == "fsa" and "dcca" in scheme:
                return val
            if route == "esef" and "17442" in scheme:
                return val
    except Exception:  # noqa: BLE001
        pass
    # Filename fallback
    if route == "fsa":
        m = _CVR_8DIGIT_RE.search(path_obj.stem)
        return m.group(1) if m else path_obj.stem
    return path_obj.stem


def _dk_fsa_pipeline(
    xml_bytes: bytes,
    entity_id: str,
    lei,
    *,
    storage: Storage,
    out: dict,
    coverage: list[dict],
    write: bool,
) -> None:
    """Parse, map, and emit rows for one DK-GAAP FSA XBRL source (Path B).

    Raises on any parse or mapping error — callers must wrap in ``try/except``.
    """
    from .dk_fsa_xbrl import parse_fsa_facts
    from .concepts_dk import map_fsa_facts

    parsed = parse_fsa_facts(xml_bytes)
    mapped = map_fsa_facts(parsed)

    # DK-GAAP FSA maps the maturity split of TOTAL liabilities -> gearing proxy.
    # _emit_mapped surfaces a LEI entity_id into the `lei` column (M1) for free.
    _emit_mapped(
        mapped, entity_id, lei, None, {"cvr": entity_id, "lei": lei},
        country="DK", source="erst-fsa", form="erst-fsa",
        leverage_basis=_BASIS_TOTAL_LIABILITIES,
        storage=storage, out=out, coverage=coverage, write=write,
    )


def _dk_esef_pipeline(
    xml_bytes: bytes,
    entity_id: str,
    lei,
    *,
    storage: Storage,
    out: dict,
    coverage: list[dict],
    write: bool,
) -> None:
    """Parse, map, and emit rows for one ESEF IFRS XBRL source (Path A).

    Reuses ``map_dk_esef`` → ``summaries_from_flat(IFRS_CONCEPTS)`` — 100 %
    EU-pillar reuse, borrowings-based ``debt_to_equity`` for free.
    source="erst-ifrs".  Raises on errors — callers must wrap in ``try/except``.
    """
    from .concepts_dk import map_dk_esef

    summaries = map_dk_esef(xml_bytes)

    cov_base: dict = {"cvr": entity_id, "lei": lei}

    if not summaries:
        coverage.append({**cov_base, "status": "no-financials"})
        out["no_financials"] += 1
        return

    # M1: on the ESEF (_from_files) path entity_id is the filer's LEI (ISO-17442).
    # On the live-API path (build_dk_financials) entity_id is the CVR, so
    # _lei_or_none(entity_id) returns None and `lei` falls back to the resolved LEI.
    row_lei = lei or _lei_or_none(entity_id)
    rows: list[dict] = []
    n = 0
    for s in summaries:
        # Build a thin ``mapped``-compatible dict to satisfy ``_base()``'s keys.
        # The PeriodSummary already carries period_end/currency/values/derived.
        mapped = {
            "basis": "company",
            "currency": s.currency,
            "period_end": s.period_end.isoformat(),
        }
        base = _base(entity_id, row_lei, mapped, s, country="DK", source="erst-ifrs")
        rows.extend(rows_from_base(base, s))
        n += 1

    cov = dict(cov_base)
    # DK-ESEF reuses the IFRS engine: NoncurrentBorrowings + CurrentBorrowings ->
    # real borrowings-based leverage (same as the EU pillar).
    _emit_entity_rows(entity_id, rows, n, cov, storage, out, coverage, write=write,
                      leverage_basis=_BASIS_BORROWINGS)


def _select_best_dk_doc(
    filings: list[dict],
) -> "tuple[str, str] | tuple[None, None]":
    """Return (route, url) for the best document URL found across all filings.

    Scans every filing's ``dokumenter`` list (filings assumed newest-first) and
    collects the first ESEF URL and the first FSA URL.  Returns ESEF when one
    exists; falls back to FSA otherwise.  Returns ``(None, None)`` when nothing
    routable is found.

    This preference is critical for **listed companies** whose annual-report
    filing carries both an ``AARSRAPPORT`` (DK-GAAP management-review, XML but
    no balance-sheet / P&L facts) *and* an ``AARSRAPPORT_ESEF`` (the real IFRS
    financials).  The old code picked the first routable document — typically the
    FSA one — and silently yielded no-financials.  Preferring ESEF guarantees
    Path A (``source="erst-ifrs"``) is always used for listed issuers while the
    FSA fallback is preserved for private companies that only file DK-GAAP.
    """
    from .virk_api import route_document

    esef_url: "str | None" = None
    fsa_url: "str | None" = None

    for filing in filings:
        for doc in filing.get("dokumenter", []):
            r = route_document(doc)
            url = doc.get("dokumentUrl")
            if not url:
                continue
            if r == "esef" and esef_url is None:
                esef_url = url
            elif r == "fsa" and fsa_url is None:
                fsa_url = url

    if esef_url is not None:
        return "esef", esef_url
    if fsa_url is not None:
        return "fsa", fsa_url
    return None, None


def build_dk_financials_from_files(
    paths,
    *,
    config: Config,
    write: bool = True,
) -> dict:
    """Parse a list of local Virk XBRL .xml files → the curated schema.

    Routes each file to Path A (ESEF/IFRS, ``source="erst-ifrs"``) or Path B
    (FSA/DK-GAAP, ``source="erst-fsa"``) by sniffing the first 8 KB for the
    ``ifrs-full`` or ``xbrl.dcca.dk/fsa`` namespace marker.

    entity_id extraction:

    * **FSA**: ``xbrli:identifier`` with scheme ``http://www.dcca.dk/cvr``
      → CVR (8 digits).  Falls back to first 8-digit sequence in filename stem.
    * **ESEF**: ``xbrli:identifier`` with ISO 17442 LEI scheme → LEI string.
      Falls back to filename stem.

    Parameters
    ----------
    paths:
        Iterable of file paths (str or Path) pointing to Virk XBRL .xml files.
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    """
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    for path in paths:
        out["entities"] += 1
        path_obj = Path(str(path))
        # Tentative cov_base before we know entity_id (updated once route is known).
        cov_base: dict = {"cvr": path_obj.stem, "lei": None}

        try:
            xml_bytes = path_obj.read_bytes()
            route = _route_dk_xml(xml_bytes)

            if route is None:
                coverage.append({**cov_base, "status": "no-financials",
                                 "note": "unrecognised document type (not esef/fsa)"})
                out["no_financials"] += 1
                continue

            entity_id = _dk_entity_id_from_xml(xml_bytes, path_obj, route)
            cov_base = {"cvr": entity_id, "lei": None}

            if route == "fsa":
                _dk_fsa_pipeline(
                    xml_bytes, entity_id, None,
                    storage=storage, out=out, coverage=coverage, write=write,
                )
            else:  # esef
                _dk_esef_pipeline(
                    xml_bytes, entity_id, None,
                    storage=storage, out=out, coverage=coverage, write=write,
                )

        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=cov_base.get("cvr"),
                                 source="erst", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "erst", write=write)


def build_dk_financials(
    specs,
    *,
    fetcher,
    config: Config,
    write: bool = True,
) -> dict:
    """Fetch Virk XBRL via the keyless Regnskaber API and emit the curated schema.

    Resolves each spec to a CVR via :func:`resolve_register_specs`, calls
    :func:`~.virk_api.search_virk_filings` (sorted newest-first), iterates the
    ``dokumenter`` list of each filing to find the first routable document
    (``route_document`` → ``"esef"`` or ``"fsa"``), fetches it with
    :func:`~.virk_api.fetch_virk_document` (auto-gunzip), and runs the
    appropriate pipeline (Path A or B).

    Keyless — no API key required.  Live / scale validation — rate limits,
    filing completeness — is a controller step, intentionally out of scope here.
    Unit-test with a stubbed fetcher returning fixture bytes.

    Parameters
    ----------
    specs:
        List of spec dicts (e.g. ``[{"cvr": "30830725"}]`` or ``[{"lei": "…"}]``).
    fetcher:
        A :class:`~company_corpus.http.Fetcher` (or any object exposing
        ``get_json``, ``get``, ``post_json``).
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    """
    from .virk_api import fetch_virk_document, search_virk_filings

    resolved = resolve_register_specs(specs, fetcher=fetcher)
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    for r in resolved:
        out["entities"] += 1
        cvr = r.get("cvr")
        lei = r.get("lei")
        cov_base: dict = {"cvr": cvr, "lei": lei}

        if not cvr:
            coverage.append({**cov_base, "status": "unresolved"})
            out["no_financials"] += 1
            continue

        try:
            filings = search_virk_filings(cvr, fetcher=fetcher)
            if not filings:
                coverage.append({**cov_base, "status": "no-financials"})
                out["no_financials"] += 1
                continue

            # Select best document: ESEF preferred over FSA across all filings.
            # Listed companies file both AARSRAPPORT (management-review, no
            # balance-sheet facts) and AARSRAPPORT_ESEF (real IFRS financials);
            # we must pick the ESEF first to reach Path A (source="erst-ifrs").
            route_dk, _best_url = _select_best_dk_doc(filings)
            xml_bytes_dk: "bytes | None" = (
                fetch_virk_document(_best_url, fetcher=fetcher)
                if _best_url is not None else None
            )

            if route_dk is None:
                coverage.append({**cov_base, "status": "no-financials"})
                out["no_financials"] += 1
                continue

            if xml_bytes_dk is None:
                # Virk listed a routable filing, so an empty document body is a
                # DEAD SOURCE (see virk_api's WARNING log), not an empty filer.
                _record_source_error(
                    f"Virk returned no document for CVR {cvr} ({_best_url})",
                    cov_base, entity_id=cvr, source="erst",
                    out=out, coverage=coverage)
                continue

            if route_dk == "fsa":
                _dk_fsa_pipeline(
                    xml_bytes_dk, cvr, lei,
                    storage=storage, out=out, coverage=coverage, write=write,
                )
            else:  # esef
                _dk_esef_pipeline(
                    xml_bytes_dk, cvr, lei,
                    storage=storage, out=out, coverage=coverage, write=write,
                )

        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=cvr,
                                 source="erst", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "erst", write=write)


# ---------------------------------------------------------------------------
# Estonia Äriregister (RIK) bulk-CSV register producer
# ---------------------------------------------------------------------------
# Keyless open-data — no subscription key required.  stdlib csv + zipfile only.
# The bulk CSV join (_iter_ee_reports) + concept map (_map_ee_report) handle
# parsing/mapping.  basis="company" (RIK standalone statutory accounts). EUR.
#
# net_income = TotalAnnualPeriodProfitLoss (FINAL after tax) — NEVER TotalProfitLoss.
# Liabilities-based leverage: short_term_debt=CurrentLiabilities,
# long_term_debt=NonCurrentLiabilities.
# interest_expense / interest_coverage: ALWAYS suppressed (not in RIK bulk).


def build_ee_financials_from_files(
    elem_path,
    meta_path,
    *,
    config: Config,
    write: bool = True,
    limit: int | None = None,
) -> dict:
    """Iterate EE Äriregister bulk CSVs and emit the curated schema.

    Joins the elements and metadata CSVs via :func:`iter_ee_reports`, groups all
    reports by ``registrikood``, deduplicates same-period resubmissions (last-seen
    wins — a proxy for the ``taidetud_aruanne_report_id`` supersession pointer which
    is not surfaced by the iterator), and writes **one file per entity** with ALL of
    its periods accumulated.  This mirrors the Norwegian :func:`build_register_financials`
    accumulate-and-dedupe pattern, avoiding the silent per-report overwrite that
    previously clobbered all but the last-processed period for an entity.

    Parameters
    ----------
    elem_path:
        File path (str / Path) or raw bytes of the elements CSV (or its .zip).
    meta_path:
        File path (str / Path) or raw bytes of the metadata CSV (or its .zip).
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    limit:
        Cap on the number of reports streamed from the iterator.  ``None`` = no cap.
    """
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    # --- Pass 1: stream reports up to *limit*, grouping by registrikood.
    # Deduplication key = period_end within each registrikood; last-seen report wins
    # for same-period resubmissions (mirrors _dedupe_latest for the NO producer,
    # which keeps the highest Brreg submission id — here we use last-seen order as
    # a proxy since iter_ee_reports does not surface taidetud_aruanne_report_id).
    entity_groups: dict[str, dict[str, dict]] = {}  # rk -> {period_key -> report}
    n_reports = 0

    for report in _iter_ee_reports(elem_path, meta_path):
        if limit is not None and n_reports >= limit:
            break
        n_reports += 1

        registrikood = report.get("registrikood")
        if not registrikood:
            # No registrikood: count as entity + no-financials immediately.
            out["entities"] += 1
            coverage.append({"registrikood": None, "lei": None, "status": "no-financials"})
            out["no_financials"] += 1
            continue

        period_end = report.get("period_end")
        # Sentinel for unknown period_end so distinct unknown-period reports don't
        # clobber each other.
        period_key = (
            period_end if period_end is not None
            else f"_none_{report.get('report_id', n_reports)}"
        )
        if registrikood not in entity_groups:
            entity_groups[registrikood] = {}
        entity_groups[registrikood][period_key] = report  # last-seen wins

    # --- Pass 2: emit one file per registrikood with all accumulated periods.
    for registrikood, period_map in entity_groups.items():
        out["entities"] += 1
        cov_base: dict = {"registrikood": registrikood, "lei": None}

        try:
            rows: list[dict] = []
            n_periods = 0
            suppressed_all: list = []
            had_unbalanced = False

            for period_key in sorted(period_map):
                report = period_map[period_key]
                mapped = _map_ee_report(
                    report["elements"], report["period_end"], registrikood
                )

                if mapped.get("suppressed"):
                    suppressed_all.extend(mapped["suppressed"])

                if mapped["unbalanced"]:
                    had_unbalanced = True
                    continue  # period gate failed; try next period for this entity

                if not mapped.get("values"):
                    continue  # NGO/no-financials period; try next

                s = _summary(
                    mapped, registrikood,
                    sec_form="rik",
                    accession=f"rik-{registrikood}-{mapped['period_end']}",
                )
                base = _base(registrikood, None, mapped, s, country="EE", source="rik")
                rows.extend(rows_from_base(base, s))
                n_periods += 1

            cov = dict(cov_base)
            if suppressed_all:
                cov["suppressed"] = suppressed_all

            # If no period produced rows and all failures were balance-gate rejections,
            # classify the entity as "unbalanced" rather than "no-financials".
            if not rows and had_unbalanced:
                cov["status"] = "unbalanced"
                coverage.append(cov)
                out["unbalanced"] += 1
                continue

            # EE maps CurrentLiabilities/NonCurrentLiabilities as the debt split ->
            # total-liabilities-based gearing proxy (no borrowings line in the RIK bulk).
            _emit_entity_rows(registrikood, rows, n_periods, cov, storage, out, coverage,
                              write=write, leverage_basis=_BASIS_TOTAL_LIABILITIES)

        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=registrikood,
                                 source="rik", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "rik", write=write)


def build_ee_financials(
    year: int,
    *,
    fetcher,
    config: Config,
    write: bool = True,
    limit: int | None = None,
    elem_url: str | None = None,
    meta_url: str | None = None,
) -> dict:
    """Download EE Äriregister bulk CSVs for *year* and emit the curated schema.

    Downloads the two bulk zips via :func:`download_ee_bulk` (keyless, CC-BY),
    then pipes the bytes through :func:`build_ee_financials_from_files`.

    Parameters
    ----------
    year:
        Fiscal year of interest — passed to ``download_ee_bulk`` for logging.
    fetcher:
        HTTP fetcher (exposes ``get(url) -> response`` with ``.content``).
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    limit:
        Cap on the number of reports processed.
    elem_url:
        Full URL for the elements zip (rotates with each RIK snapshot; obtain
        from avaandmed.ariregister.rik.ee/et/avaandmete-allalaadimine).
    meta_url:
        Full URL for the metadata zip.
    """
    elem_bytes, meta_bytes = _download_ee_bulk(
        year, fetcher=fetcher, elem_url=elem_url, meta_url=meta_url
    )
    return build_ee_financials_from_files(
        elem_bytes, meta_bytes, config=config, write=write, limit=limit
    )


def build_sk_financials_from_files(
    vykaz_path,
    sablona_path,
    *,
    config: Config,
    write: bool = True,
) -> dict:
    """Load a single SK registeruz.sk vykaz JSON + sablona JSON → the curated schema.

    Loads the two files from disk, runs :func:`map_sk_vykaz`, and emits the
    curated financials row set via the shared ``_emit_entity_rows`` tail.

    Parameters
    ----------
    vykaz_path:
        File path to the ``/uctovny-vykaz`` JSON (str or Path).
    sablona_path:
        File path to the ``/sablona`` JSON (str or Path).
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    """
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()

    out["entities"] += 1
    with open(vykaz_path, encoding="utf-8") as fh:
        vykaz = json.load(fh)
    with open(sablona_path, encoding="utf-8") as fh:
        sablona = json.load(fh)

    ico = (vykaz.get("obsah") or {}).get("titulnaStrana", {}).get("ico")
    entity_id = ico or Path(str(vykaz_path)).stem
    cov_base: dict = {"ico": entity_id, "lei": None}

    try:
        mapped = _map_sk_vykaz(vykaz, sablona)

        # SK maps real bank-loan borrowings → borrowings-based leverage.
        _emit_mapped(
            mapped, entity_id, None, None, cov_base,
            country="SK", source="registeruz", form="registeruz",
            leverage_basis=_BASIS_BORROWINGS,
            storage=storage, out=out, coverage=coverage, write=write,
        )

    except Exception as exc:  # noqa: BLE001 — record + skip
        _record_source_error(exc, cov_base, entity_id=entity_id,
                             source="registeruz", out=out, coverage=coverage)

    return _finalise_coverage(out, coverage, config, "registeruz", write=write)


def build_sk_financials(
    entity_ids,
    *,
    fetcher,
    config: Config,
    write: bool = True,
    limit: int | None = None,
) -> dict:
    """Traverse registeruz.sk entity IDs → fetch → accumulate → emit curated schema.

    For each numeric entity ID, fetches:
    * the entity record (/uctovna-jednotka) → ``ico`` + ``idUctovnychZavierok``
    * for each zavierka (/uctovna-zavierka) → ``idUctovnychVykazov``
    * for each vykaz (/uctovny-vykaz) + its sablona (/sablona, cached by id)

    **Accumulates all periods per IČO** (multi-year; write once per entity,
    dedupe by period_end — last-seen wins for same-period resubmissions).
    Per-entity try/except ensures one bad entity does not abort the batch.

    Parameters
    ----------
    entity_ids:
        Iterable of numeric registeruz entity IDs (or a single int).
    fetcher:
        HTTP fetcher (exposes ``get_json(url, *, params)``).
    config:
        Config instance (``data_dir`` for output).
    write:
        Persist rows + coverage (default True); False for dry-run.
    limit:
        Cap on the number of entities processed. ``None`` = no cap.
    """
    storage = Storage(config)
    coverage: list[dict] = []
    out = _make_out()
    sablona_cache: dict[int, dict] = {}  # id_sablony → sablona dict (few distinct values)

    n_entities = 0
    for entity_id in entity_ids:
        if limit is not None and n_entities >= limit:
            break
        n_entities += 1
        out["entities"] += 1
        cov_base: dict = {"entity_id": entity_id, "ico": None, "lei": None}

        try:
            entity = _sk_fetch_entity(entity_id, fetcher=fetcher)
            if not entity:
                coverage.append({**cov_base, "status": "no-financials",
                                 "note": "entity not found"})
                out["no_financials"] += 1
                continue

            ico = entity.get("ico")
            if not ico:
                coverage.append({**cov_base, "status": "no-financials",
                                 "note": "no IČO on entity record"})
                out["no_financials"] += 1
                continue
            cov_base = {"ico": ico, "lei": None}

            # --- Accumulate all periods for this IČO.
            # Dedupe key = period_end; last-seen vykaz for a given period wins.
            period_map: dict[str, dict] = {}   # period_key → mapped
            had_unbalanced = False
            suppressed_all: list = []

            for zavierka_id in (entity.get("idUctovnychZavierok") or []):
                zavierka = _sk_fetch_zavierka(zavierka_id, fetcher=fetcher)
                if not zavierka:
                    continue
                for vykaz_id in (zavierka.get("idUctovnychVykazov") or []):
                    vykaz = _sk_fetch_vykaz(vykaz_id, fetcher=fetcher)
                    if not vykaz:
                        continue
                    id_sablony = vykaz.get("idSablony")
                    if id_sablony is None:
                        continue
                    # Cache sablony — there are very few distinct ids.
                    sablona = sablona_cache.get(id_sablony)
                    if sablona is None:
                        sablona = _sk_fetch_sablona(id_sablony, fetcher=fetcher)
                        if sablona:
                            sablona_cache[id_sablony] = sablona
                    if not sablona:
                        continue

                    mapped = _map_sk_vykaz(vykaz, sablona)
                    if mapped.get("suppressed"):
                        suppressed_all.extend(mapped["suppressed"])
                    if mapped["unbalanced"]:
                        had_unbalanced = True
                        continue
                    if not mapped.get("values"):
                        continue
                    period_end = mapped.get("period_end")
                    period_key = (
                        period_end if period_end is not None
                        else f"_none_{vykaz_id}"
                    )
                    period_map[period_key] = mapped   # last-seen wins

            # --- Build rows from all accumulated periods (sorted by period_end).
            rows: list[dict] = []
            n_periods = 0
            for period_key in sorted(period_map):
                mapped = period_map[period_key]
                s = _summary(
                    mapped, ico,
                    sec_form="registeruz",
                    accession=f"registeruz-{ico}-{mapped['period_end']}",
                )
                base = _base(ico, None, mapped, s, country="SK", source="registeruz")
                rows.extend(rows_from_base(base, s))
                n_periods += 1

            cov = dict(cov_base)
            if suppressed_all:
                cov["suppressed"] = suppressed_all

            # If no period produced rows and all failures were balance-gate rejections,
            # classify as "unbalanced" rather than "no-financials".
            if not rows and had_unbalanced:
                cov["status"] = "unbalanced"
                coverage.append(cov)
                out["unbalanced"] += 1
                continue

            # SK maps real bank borrowings → borrowings-based leverage.
            _emit_entity_rows(ico, rows, n_periods, cov, storage, out, coverage,
                              write=write, leverage_basis=_BASIS_BORROWINGS)

        except Exception as exc:  # noqa: BLE001 — record + skip, keep the batch going
            _record_source_error(exc, cov_base, entity_id=ico,
                                 source="registeruz", out=out, coverage=coverage)
            continue

    return _finalise_coverage(out, coverage, config, "registeruz", write=write)
