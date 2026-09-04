"""Orchestrator for the European acquisition (Pillar A).

resolve universe -> dispatch each entity to its country OAM backend + the
filings.xbrl.org complement -> merge/dedupe -> download every file -> write entity
index, manifests, and the coverage report.
"""
from __future__ import annotations

import json

from ..config import Config
from .dispatcher import merge_documents
from .download import download_document
from .entities import Entity, resolve_entities
from .reconcile import reconcile
from .sources.filings_org import FilingsXbrlOrg
from .sources.oam_be import StoriBE
from .sources.oam_ch import DisclosureCH
from .sources.oam_de import BundesanzeigerDE
from .sources.oam_dk import OamDK
from .sources.oam_es import CnmvES
from .sources.oam_euronext import EURONEXT_MICS, EuronextSource, _LISTING_MIC
from .sources.oam_fi import OamFI
from .sources.oam_fr import InfoFinanciereFR
from .sources.oam_gb import NsmGB
from .sources.oam_it import OneInfoIT
from .sources.oam_nl import AfmNL
from .sources.oam_se import OamSE
from .sources.oam_no import NewsWebNO

# Increment A+B+C backends. Entities whose country has no backend resolve but discover
# 0 docs -> the coverage report flags them as "no-documents" (deliberate: never
# silently partial).
COUNTRY_BACKENDS = {
    "BE": StoriBE,
    "CH": DisclosureCH,
    "DE": BundesanzeigerDE,
    "DK": OamDK,
    "ES": CnmvES,
    "FI": OamFI,
    "FR": InfoFinanciereFR,
    "GB": NsmGB,
    # Ireland: the FCA NSM is the de-facto OAM for Irish issuers (verified live —
    # it holds even small Euronext-Growth-Dublin names by LEI), and the Euronext
    # Dublin per-issuer feed is empty. So Irish issuers resolve through the same
    # LEI-keyed NSM backend.
    # Caveats (tracked, not silent): (1) this is a UK mechanism serving an EU
    # jurisdiction — an Irish-only issuer that never passported into the NSM would
    # have no national EU backend and surface as no-documents in reconcile (never
    # a silent gap); the Euronext Dublin market feed remains a future fallback.
    # (2) Give each LEI a single entity in the universe: a dual-listed issuer
    # present as both a GB and an IE entity would yield the same NSM docs under
    # two country labels (first-wins dedup keeps one, order-dependent).
    "IE": NsmGB,
    "IT": OneInfoIT,
    "NL": AfmNL,
    "SE": OamSE,
    "NO": NewsWebNO,
}


def _has_oslo_notice(per_backend) -> bool:
    """True if the Euronext listing probe returned an Oslo (``OSL_``) notice for
    the entity — i.e. it is confirmed admitted to Oslo Børs. The notice number is
    exchange-prefixed (``OSL_…``/``AMS_…``/``LIS_…``), so this is an exchange
    signal, not a name guess."""
    return any(
        d.source == "euronext"
        and str(d.native_meta.get("notice_number", "")).upper().startswith("OSL")
        for docs in per_backend for d in docs
    )


def acquire(specs, *, fetcher, config: Config, download: bool = True) -> dict:
    entities = resolve_entities(specs, fetcher=fetcher)
    _write_entity_index(entities, config)

    all_docs, errors = [], []
    for e in entities:
        if not e.lei:
            continue
        backends = []
        cls = COUNTRY_BACKENDS.get(e.country)
        if cls:
            backends.append(cls(fetcher=fetcher, config=config))
        backends.append(FilingsXbrlOrg(fetcher=fetcher, config=config))
        # Euronext is a cross-market complement (corporate-event notices). It is
        # listed AFTER the national backend so that on any genuine overlap the
        # more-complete national document wins the first-occurrence dedup.
        if e.country in EURONEXT_MICS:
            backends.append(EuronextSource(fetcher=fetcher, config=config))
        elif COUNTRY_BACKENDS.get(e.country) is None:
            # The home country has no backend, but the issuer may be LISTED on a
            # Euronext venue (e.g. a Bermuda/Luxembourg issuer on Oslo/Amsterdam).
            # The notices feed is ISIN-keyed, so query by the entity's ISINs and
            # verify the issuer name per notice (rejects market-wide noise).
            backends.append(EuronextSource(fetcher=fetcher, config=config,
                                           force_mic=_LISTING_MIC))
        per_backend = []
        for b in backends:
            try:
                per_backend.append(b.discover(e))
            except Exception as exc:  # noqa: BLE001
                per_backend.append([])
                errors.append({"source": "acquire", "context": "discover",
                               "entity": e.lei, "error": str(exc)})
            errors.extend(getattr(b, "errors", []))
        # Corroborated rich Oslo coverage: when the Euronext probe returned an
        # Oslo (OSL_) notice for a non-Norwegian issuer, it is confirmed listed on
        # Oslo Børs — so a name match to Oslo NewsWeb is backed by a second,
        # independent Oslo signal and is safe from a coincidental same-name bind
        # (NewsWeb has no ISIN to key on, so name alone would otherwise be a guess).
        if e.country != "NO" and _has_oslo_notice(per_backend):
            no_b = NewsWebNO(fetcher=fetcher, config=config)
            try:
                per_backend.append(no_b.discover(e))
            except Exception as exc:  # noqa: BLE001
                per_backend.append([])
                errors.append({"source": "acquire", "context": "discover",
                               "entity": e.lei, "error": str(exc)})
            errors.extend(no_b.errors)
        all_docs.extend(merge_documents(per_backend))

    manifests = 0
    download_errors = 0
    deduped_by_bytes = 0
    kept_docs = all_docs
    if download:
        # Authoritative cross-backend dedup, confirmed by bytes: same company +
        # same publication-day + a byte-identical file = the same disclosure. The
        # file-name merge above cannot see this when backends name the file
        # differently (e.g. a national OAM vs the Euronext complement); the sha256
        # is the ground truth. doc_type is deliberately NOT in the key — two
        # backends routinely classify the same file differently (Euronext "other"
        # vs a national "annual_report"), and identical bytes already prove
        # identity. First occurrence wins (national backend listed first).
        kept_docs = []
        seen_bytes: dict[tuple, str] = {}  # (lei, day, sha256) -> doc_id
        for d in all_docs:
            man = download_document(d, fetcher=fetcher, config=config)
            day = (d.published_ts or "")[:10]
            shas = [f["sha256"] for f in man.get("files", []) if f.get("sha256")]
            sig = (d.lei, day)
            if day and shas and any((*sig, s) in seen_bytes for s in shas):
                _discard_download(man, config)
                deduped_by_bytes += 1
                continue
            for s in shas:
                seen_bytes[(*sig, s)] = d.doc_id
            manifests += 1
            kept_docs.append(d)
            for f in man.get("files", []):
                if "error" in f:
                    download_errors += 1
                    errors.append({"source": "acquire", "context": "download",
                                   "doc_id": d.doc_id, "file": f.get("name"),
                                   "error": f["error"]})

    cov = reconcile(entities, kept_docs)
    cov_path = config.data_dir / "reports" / "eu_coverage.jsonl"
    cov_path.parent.mkdir(parents=True, exist_ok=True)
    cov_path.write_text("\n".join(json.dumps(r, default=str) for r in cov))

    return {"entities": len(entities), "documents": len(kept_docs),
            "manifests": manifests, "deduped_by_bytes": deduped_by_bytes,
            "download_errors": download_errors,
            "coverage_path": str(cov_path), "errors": errors}


def _discard_download(manifest: dict, config: Config) -> None:
    """Remove a byte-confirmed duplicate's downloaded files and manifest.

    Best-effort: the duplicate was downloaded only to confirm its bytes, so its
    artefacts are deleted to avoid storing the same disclosure twice. Different
    doc_id => its own directory, so this never touches the kept document.
    """
    lei = manifest.get("lei") or "UNRESOLVED"
    doc_id = manifest.get("doc_id")
    for f in manifest.get("files", []):
        rel = f.get("path")
        if rel:
            try:
                (config.data_dir / rel).unlink(missing_ok=True)
            except OSError:
                pass
    if doc_id:
        try:
            (config.data_dir / "manifest" / lei / f"{doc_id}.json").unlink(missing_ok=True)
        except OSError:
            pass


def _write_entity_index(entities: list[Entity], config: Config) -> None:
    path = config.data_dir / "universe" / "eu_entities.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({
        "lei": e.lei, "name": e.name, "country": e.country, "isins": list(e.isins),
        "tickers": list(e.tickers), "resolution": e.resolution}) for e in entities))
