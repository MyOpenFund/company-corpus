"""EU Pillar B producer — structured IFRS financials from ESEF (filings.xbrl.org).

filings.xbrl.org exposes each ESEF filing's facts as OIM xBRL-JSON (``json_url``):
the "European companyfacts". We union an issuer's filings into one ``flat`` dict and
run the shared engine with the IFRS concept pack, writing the SEC-unified schema.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..config import Config
from ..financials import attach_ttm_from_flat, make_row_base, rows_from_base, summaries_from_flat
from ..storage import Storage
from ..xbrl import IFRS_CONCEPTS, IFRS_CONCEPTS_BY_KEY, flatten_oim_json
from .arelle_esef import oim_from_esef_zip
from .entities import Entity, resolve_entities
from .sources.filings_org import FilingsXbrlOrg

log = logging.getLogger(__name__)


def _record_error(errors: "list[dict] | None", lei: "str | None", message: str) -> None:
    """Log a dead ESEF source at WARNING and, when collecting, append one
    timestamped item in the shape every pillar's ``error_items`` uses."""
    log.warning("esef: source error for %s: %s", lei, message)
    if errors is None:
        return
    errors.append({
        "entity_id": lei, "source": "esef", "error": message,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def facts_for_entity(
    entity: Entity, *, fetcher, errors: "list[dict] | None" = None,
) -> dict[str, list[dict]]:
    """Union the OIM-JSON facts across all of the entity's filings.xbrl.org filings.

    Each annual report carries the current + prior-year comparative; the union yields
    a multi-year series, and the engine's latest-filed rule resolves restatements.

    A filing whose facts JSON cannot be fetched is still skipped (one dead report
    must not abort the issuer), but it is no longer silent: it is logged at WARNING
    and, when the caller passes ``errors``, appended there as
    ``{entity_id, source, error, ts}`` so the run report and the discovery-error
    trail can tell "the aggregator failed" from "the issuer filed nothing".

    The same holds one level up: the backend swallows a failed filings LISTING
    into ``src.errors`` and returns no documents, so those recorded errors are
    folded into ``errors`` too — a dead aggregator yields an empty ``flat`` AND
    an error, never a look-alike "no filings". (A 404 "not indexed" is a note
    on the backend, not an error, and stays a genuine no-filings.)
    """
    flat: dict[str, list[dict]] = {}
    if not entity.lei:
        return flat
    src = FilingsXbrlOrg(fetcher=fetcher)
    docs = src.discover(entity)
    for e in src.errors:
        _record_error(errors, entity.lei, f"{e['context']}: {e['url']}: {e['error']}")
    for doc in docs:
        meta = doc.native_meta or {}
        jf = next((f for f in doc.files if f.get("kind") == "json_url" and f.get("url")), None)
        if not jf:
            continue
        try:
            report = fetcher.get_json(jf["url"])
        except Exception as exc:  # noqa: BLE001 — skipped, never fatal, never silent
            _record_error(errors, entity.lei, f"{jf['url']}: {exc}")
            continue
        if not report:           # a None/empty body is skipped, never fatal
            _record_error(errors, entity.lei, f"{jf['url']}: empty facts document")
            continue
        part = flatten_oim_json(
            report,
            filed=str(meta.get("date_added") or doc.published_ts or "")[:10],
            form=doc.doc_type,
            accn=str(meta.get("fxo_id") or doc.doc_id),
        )
        for tag, pts in part.items():
            flat.setdefault(tag, []).extend(pts)
    return flat


def arelle_facts_for_entity(entity: "Entity", *, config) -> dict[str, list[dict]]:
    """Union the facts from an entity's LOCAL ESEF .zip packages (acquisition
    manifests, kind="esef"), parsed with Arelle. Mirrors facts_for_entity but
    offline. A zip that fails to parse is skipped, never fatal."""
    flat: dict[str, list[dict]] = {}
    if not entity.lei:
        return flat
    mdir = config.data_dir / "manifest" / entity.lei
    if not mdir.is_dir():
        return flat
    for mpath in sorted(mdir.glob("*.json")):
        try:
            man = json.loads(mpath.read_text())
        except Exception:        # noqa: BLE001
            continue
        filed = str(man.get("published_ts") or "")[:10]
        form = man.get("doc_type") or "annual_report"
        for fmeta in man.get("files", []):
            if fmeta.get("kind") != "esef" or not fmeta.get("path"):
                continue
            zip_path = config.data_dir / fmeta["path"]
            try:
                report = oim_from_esef_zip(str(zip_path))
                part = flatten_oim_json(report, filed=filed, form=form, accn=man.get("doc_id") or mpath.stem)
            except Exception:    # noqa: BLE001  (bad/unparseable package skipped)
                continue
            for tag, pts in part.items():
                flat.setdefault(tag, []).extend(pts)
    return flat


def _eu_base(lei: str, country: "str | None", summary) -> dict:
    """The canonical RowBase, EU/ESEF-filled: entity_id = LEI (id_scheme ``lei``),
    source ``esef``, form = the ESEF doc_type, accession = the filing's fxo_id/doc_id.
    ``country`` is the issuer's GLEIF jurisdiction (real data, not derived from the
    LEI prefix); is_financial is None (ESEF carries no industry classification)."""
    return make_row_base(
        summary, entity_id=lei, id_scheme="lei", lei=lei, country=country,
        source="esef", form=summary.sec_form, accession=summary.accession,
        sic=None, is_financial=summary.is_financial, basis=None)


def build_eu_financials(specs, *, fetcher, config: Config, write: bool = True, use_arelle: bool = False) -> dict:
    """Resolve specs -> IFRS financials -> data/financials_eu/<LEI>.jsonl (SEC schema).

    Coverage (with/without financials) is written to reports/eu_financials_coverage.jsonl;
    an unresolved or unindexed issuer is recorded there, never silently dropped.

    ``out["errors"]`` counts the filings whose facts could not be fetched and
    ``out["error_items"]`` carries one timestamped record each — a dead
    aggregator therefore degrades the run instead of masquerading as an issuer
    with no financials. Errors are counted per *filing* while ``entities`` is
    per *issuer*, so in the run report ``docs_failed`` may exceed ``docs_seen``
    for an issuer with several dead filings.

    ``out["unresolved"]`` counts the specs that resolved to no LEI (GLEIF had no
    record — or GLEIF itself was unreachable, which looks identical here) and
    ``out["unresolved_specs"]`` lists them, the input specs in input order, so
    the CLI can fail a run in which NOTHING resolved instead of reporting a
    green "no financials".
    """
    entities = resolve_entities(specs, fetcher=fetcher)
    storage = Storage(config)
    coverage: list[dict] = []
    error_items: list[dict] = []
    unresolved_specs: list[dict] = []
    out = {"entities": 0, "with_financials": 0, "no_financials": 0, "periods": 0,
           "paths": [], "errors": 0, "error_items": error_items,
           "unresolved": 0, "unresolved_specs": unresolved_specs}
    for i, ent in enumerate(entities):
        out["entities"] += 1
        if not ent.lei:
            coverage.append({"lei": None, "name": ent.name, "resolution": ent.resolution,
                             "status": "unresolved"})
            out["no_financials"] += 1
            out["unresolved"] += 1
            # resolve_entities yields one Entity per spec, in order; the fallback
            # only guards a resolver stub that returns a different shape.
            unresolved_specs.append(specs[i] if i < len(specs) else {"name": ent.name})
            continue
        n_errors_before = len(error_items)
        flat = facts_for_entity(ent, fetcher=fetcher, errors=error_items)
        own_errors = error_items[n_errors_before:]
        arelle_flat = arelle_facts_for_entity(ent, config=config) if use_arelle else {}
        for tag, pts in arelle_flat.items():
            flat.setdefault(tag, []).extend(pts)
        # E-I4: the ESEF pillar has no industry classification, so the issuer's
        # sector is UNKNOWN -- pass sector_known=False so is_financial resolves to
        # None and the engine does NOT falsely assert sector-relevance on the
        # bank/insurer-sensitive metrics (ESEF is bank-heavy). Deriving a real
        # financial flag from NACE (via GLEIF) is a deferred feature; until then
        # "unknown" is the honest value (mirrors is_financial=None in _eu_base).
        summaries = summaries_from_flat(flat, concepts=IFRS_CONCEPTS, company=ent.name,
                                        company_current=ent.name, sic=None, sector_known=False)
        attach_ttm_from_flat(flat, summaries, concepts_by_key=IFRS_CONCEPTS_BY_KEY)
        if not summaries:
            if own_errors:
                # Every filing we knew about failed to load: the SOURCE is dead
                # for this issuer, which is not the same fact as "filed nothing".
                coverage.append({"lei": ent.lei, "name": ent.name,
                                 "status": "source-error",
                                 "error": own_errors[0]["error"]})
                continue
            coverage.append({"lei": ent.lei, "name": ent.name, "status": "no-financials"})
            out["no_financials"] += 1
            continue
        rows: list[dict] = []
        for s in summaries:
            rows.extend(rows_from_base(_eu_base(ent.lei, ent.country or None, s), s))
        out["periods"] += len(summaries)
        out["with_financials"] += 1
        if write:
            out["paths"].append(storage.write_eu_financials_table(ent.lei, rows))
        cov_ok = {"lei": ent.lei, "name": ent.name, "status": "ok",
                  "periods": len(summaries), "fy_range": [summaries[-1].fy, summaries[0].fy]}
        if use_arelle:
            cov_ok["arelle"] = bool(arelle_flat)
        coverage.append(cov_ok)
    out["errors"] = len(error_items)
    cov_path = config.data_dir / "reports" / "eu_financials_coverage.jsonl"
    if write:
        cov_path.parent.mkdir(parents=True, exist_ok=True)
        cov_path.write_text("\n".join(json.dumps(r, default=str) for r in coverage))
        out["coverage_path"] = str(cov_path)
    else:
        out["coverage_path"] = None
    return out
