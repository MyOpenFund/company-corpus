"""Shared register-producer primitives (DRY core for :mod:`.financials`).

The ~13 register producers in :mod:`company_corpus.registers.financials` share
an identical output contract — the same ``out`` summary dict, the same per-source
coverage-write tail, the same RowBase construction, and (for the single-period
producers) the same ``gate → classify → emit`` body.  Those primitives live here so
the producers stay thin and no new producer can accidentally diverge from the
NO-FALSE-DATA gate ordering.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone

from ..config import Config
from ..financials import PeriodSummary, make_row_base, rows_from_base, stamp_leverage_basis
from ..storage import Storage, _atomic_write_text

log = logging.getLogger(__name__)

# ISO-17442 LEI: exactly 20 upper-case alphanumerics. Used on the DK path to
# populate the `lei` column when the entity_id is itself a LEI (ESEF filers).
_LEI_RE = re.compile(r"[A-Z0-9]{20}\Z")


def _lei_or_none(entity_id: str) -> "str | None":
    """Return ``entity_id`` when it is a syntactic LEI (ISO-17442), else None."""
    return entity_id if _LEI_RE.match(entity_id or "") else None


# ISO-4217 currency code: exactly three upper-case letters.  Shared by the BE/UK
# concept mappers to validate the reporting currency.
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


def _tol(scale: float) -> float:
    """Absolute tolerance for a balance identity at magnitude ``scale``:
    ``max(2, 0.005 * |scale|)`` — 0.5%, but never tighter than 2 currency units
    (so tiny micro-entity filings are not tripped by rounding)."""
    return max(2.0, 0.005 * abs(scale))


# Brreg's standard layout exposes assets only as the aggregate `sumAnleggsmidler` and
# never breaks out goodwill / intangibles, so the engine's tangible_book_value
# (= common equity − goodwill − intangibles, both defaulting to 0) collapses to `equity`
# and would silently OVERSTATE true TBV for any obligor carrying intangibles. We
# structurally cannot compute it from the register, so we suppress it (and its per-share
# form, already absent for want of a share count) rather than emit a misleading figure.
_SUPPRESSED_CONCEPTS = {"tangible_book_value", "tangible_book_value_per_share"}


def _summary(
    mapped: dict, name: str,
    *, sec_form: str = "brreg", accession: str | None = None,
) -> PeriodSummary:
    """Build a PeriodSummary from a ``mapped`` dict (NO or UK)."""
    pe = date.fromisoformat(mapped["period_end"])
    acc = accession if accession is not None else f"{sec_form}-{pe.isoformat()}"
    return PeriodSummary(
        period_end=pe, frequency="annual", publication_date=None, sec_form=sec_form,
        accession=acc, company=name, company_current=name,
        values=mapped["values"], currency=mapped["currency"], sic=None)


# ARCH-C1: which national identifier a register's ``entity_id`` is, keyed by the
# register ``source``. erst-ifrs is special — its entity_id is the filer's LEI on the
# from-files path but the CVR on the live-API path — so ``_base`` resolves ``id_scheme``
# to "lei" whenever the entity_id is a syntactic LEI, and only otherwise consults this map.
_SOURCE_ID_SCHEME: dict[str, str] = {
    "brreg": "orgnr", "companies_house": "companies_house", "bnb": "kbo",
    "lbr": "rcs", "prh": "ytunnus", "erst-fsa": "cvr", "erst-ifrs": "cvr",
    "rik": "registrikood", "registeruz": "ico",
}


def _base(
    entity_id: str, lei, mapped: dict, summary: PeriodSummary,
    *, country: str, source: str,
) -> dict:
    """Build the canonical RowBase (identity + provenance + period columns), register-
    filled: entity_id = the national number (or the LEI, for ESEF filers), id_scheme
    per :data:`_SOURCE_ID_SCHEME`, source = the register tag, accession = the summary's
    computed accession. sic / is_financial are None (registers carry no industry
    classification); basis is the register's company/consolidated flag. ``frequency``
    now comes from the summary (annual) rather than being hardcoded (ARCH-C1)."""
    id_scheme = "lei" if _lei_or_none(entity_id) else _SOURCE_ID_SCHEME.get(source)
    return make_row_base(
        summary, entity_id=entity_id, id_scheme=id_scheme, lei=lei, country=country,
        source=source, form=None, accession=summary.accession,
        sic=None, is_financial=None, basis=mapped["basis"])


def _emit_entity_rows(
    entity_id: str, rows: list[dict], n_periods: int,
    cov_base: dict, storage: Storage, out: dict, coverage: list[dict],
    *, write: bool, leverage_basis: "str | None" = None,
) -> None:
    """Shared tail: write the financials table, update counters, append coverage entry.

    Handles both the ``no-financials`` (empty rows) and ``ok`` paths. Error and
    ``unbalanced`` paths are handled by the individual producers before calling here.

    Parameters
    ----------
    entity_id:  Key for ``write_register_financials_table`` and ``paths``.
    rows:       Pre-built row list from ``rows_from_base``; may be empty.
    n_periods:  Number of source periods that contributed rows (for the coverage entry).
    cov_base:   Dict of coverage-identifying fields (e.g. ``{"orgnr": …}`` for NO,
                ``{"ch_number": …}`` for UK); ``status`` and ``periods`` are added here.
    storage:    Storage instance for ``write_register_financials_table``.
    out:        Mutable summary dict; ``no_financials`` / ``with_financials`` / ``periods``
                / ``paths`` are updated in-place.
    coverage:   Mutable list; one entry is appended.
    write:      When False, skip the disk write and ``paths`` update.
    leverage_basis:  ``"borrowings"`` or ``"total_liabilities"`` — the producer
                declares which debt definition backs the leverage-derived rows; it
                is stamped onto them (see :func:`stamp_leverage_basis`). ``None``
                (the default, e.g. FI which emits no leverage rows) leaves the
                field absent.
    """
    # Single choke point: drop concepts that are structurally unprovable from any
    # register source.  Filtering here — rather than in each individual producer —
    # means no new producer can accidentally emit them.
    rows = [row for row in rows if row.get("concept") not in _SUPPRESSED_CONCEPTS]
    # C1: stamp the leverage basis onto the leverage-derived rows (no-op if None).
    stamp_leverage_basis(rows, leverage_basis)
    if not rows:
        coverage.append({**cov_base, "status": "no-financials"})
        out["no_financials"] += 1
        return
    out["periods"] += n_periods
    out["with_financials"] += 1
    if write:
        out["paths"].append(storage.write_register_financials_table(entity_id, rows))
    coverage.append({**cov_base, "status": "ok", "periods": n_periods})


def _emit_mapped(
    mapped: dict, entity_id: str, lei, name, cov_base: dict,
    *, country: str, source: str, form: str, leverage_basis: "str | None",
    storage: Storage, out: dict, coverage: list[dict], write: bool,
) -> None:
    """Shared single-period ``gate → classify → emit`` body.

    Runs the NO-FALSE-DATA gate in the one correct order — ``unbalanced`` first (an
    unbalanced filing yields ``values={}``, so it must be caught before the empty-values
    branch), then ``no-financials``, then emit — so no single-period register caller can
    get the ordering wrong.  Callers with their own pre-gate (e.g. UK's ``mapped is None``
    check) run it *before* calling here.  Multi-period producers (NO/EE/SK-live/DK-ESEF)
    accumulate across periods and call :func:`_emit_entity_rows` directly instead.

    Parameters
    ----------
    mapped:    The concept-mapper output (must have ``unbalanced``; may have
               ``values`` / ``suppressed``).
    entity_id: National identifier; the table key and the accession/lei subject.
    lei:       Resolved LEI (or None). The ``lei`` column falls back to
               ``_lei_or_none(entity_id)`` when the entity_id is itself a LEI (the
               DK-FSA case) — a no-op for every register whose entity_id is a
               national number.
    name:      Company name for the summary; ``name or entity_id`` is used.
    cov_base:  Producer-specific coverage identity (e.g. ``{"be_number": …}``).
    country:   ISO-2 country for the RowBase.
    source:    Row ``source`` tag AND the summary ``sec_form`` (equal for every
               single-period register).
    form:      Accession prefix — ``f"{form}-{entity_id}-{period_end}"`` — usually
               ``== source`` but distinct for UK (``"ch"`` vs ``"companies_house"``).
    leverage_basis:  ``"borrowings"`` / ``"total_liabilities"`` / None — stamped
               onto the leverage-derived rows (see :func:`_emit_entity_rows`).
    """
    if mapped["unbalanced"]:
        cov = {**cov_base, "status": "unbalanced"}
        if mapped.get("suppressed"):
            cov["suppressed"] = mapped["suppressed"]
        coverage.append(cov)
        out["unbalanced"] += 1
        return

    if not mapped.get("values"):
        cov = {**cov_base, "status": "no-financials"}
        if mapped.get("suppressed"):
            cov["suppressed"] = mapped["suppressed"]
        coverage.append(cov)
        out["no_financials"] += 1
        return

    s = _summary(
        mapped, name or entity_id,
        sec_form=source,
        accession=f"{form}-{entity_id}-{mapped['period_end']}",
    )
    # When the entity_id is itself a LEI, surface it in the `lei` column too (DK
    # FSA); a no-op for every register whose entity_id is a national number.
    row_lei = lei or _lei_or_none(entity_id)
    base = _base(entity_id, row_lei, mapped, s, country=country, source=source)
    rows = list(rows_from_base(base, s))

    cov = dict(cov_base)
    if mapped.get("suppressed"):
        cov["suppressed"] = mapped["suppressed"]
    _emit_entity_rows(entity_id, rows, 1, cov, storage, out, coverage,
                      write=write, leverage_basis=leverage_basis)


def _make_out() -> dict:
    """Fresh, zeroed producer summary dict (identical across every register).

    ``unbalanced`` is always present (some producers historically omitted it; it
    should always have been there — a balance-gate rejection is a distinct outcome
    from ``no_financials``).

    ``source_errors`` counts the subset of ``errors`` that are DEAD SOURCES (the
    register could not be reached / returned an explicit failure) rather than bad
    data, and ``error_items`` carries one timestamped record per error so the CLI
    can feed the run report and append to the discovery-error trail.
    """
    return {
        "entities": 0, "with_financials": 0, "no_financials": 0,
        "unbalanced": 0, "errors": 0, "source_errors": 0, "periods": 0,
        "paths": [], "error_items": [],
    }


def _record_source_error(
    exc, cov_base: dict, *, entity_id, source: str, out: dict, coverage: list[dict],
) -> None:
    """Record a dead source: coverage row + counters + a timestamped error item.

    The coverage status is ``source-error`` — deliberately NOT ``no-financials``:
    "we could not read the register" and "the issuer filed nothing" are different
    facts, and only the second one is a property of the issuer. ``exc`` may be an
    exception or any message; it is stringified into the row's ``error`` field.
    """
    msg = str(exc)
    coverage.append({**cov_base, "status": "source-error", "error": msg})
    out["errors"] += 1
    out["source_errors"] = out.get("source_errors", 0) + 1
    out.setdefault("error_items", []).append({
        "entity_id": entity_id,
        "source": source,
        "error": msg,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    log.warning("%s: source error for %s: %s", source, entity_id, msg)


def _finalise_coverage(
    out: dict, coverage: list[dict], config: Config, source: str, *, write: bool,
) -> dict:
    """Write the per-source coverage file and stamp ``coverage_path`` onto ``out``.

    The coverage file is ``register_coverage_<source>.jsonl`` under ``reports/`` —
    one JSON object per line.  When ``write`` is False (dry-run) nothing is written
    and ``coverage_path`` is ``None``.  Returns ``out`` so producers can
    ``return _finalise_coverage(...)``.

    ``source`` is the coverage-file suffix (e.g. ``"brreg"``, ``"erst"``), which is
    not always the same as the row ``source`` tag — DK writes a single
    ``register_coverage_erst.jsonl`` for both ``erst-fsa`` and ``erst-ifrs`` rows.
    """
    if write:
        cov_path = config.data_dir / "reports" / f"register_coverage_{source}.jsonl"
        _atomic_write_text(
            cov_path, "\n".join(json.dumps(c, default=str) for c in coverage))
        out["coverage_path"] = str(cov_path)
    else:
        out["coverage_path"] = None
    return out
