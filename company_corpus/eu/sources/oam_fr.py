"""info-financiere.gouv.fr (AMF) backend — Opendatasoft Explore v2.1 records API.

Recon-confirmed: dataset `flux-amf-new-prod` (~531k records). The v2.1 `/records`
response is `{"total_count": N, "results": [ <flat field dict>, ... ]}` (results
items ARE the field dicts; no record/fields nesting). Confirmed field names below.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from ..documents import Document
from ..entities import Entity
from ..oam_base import OamSource

# LEIs and ISINs are uppercase alphanumeric; reject anything else to prevent
# stray quotes from breaking or altering the ODS where-clause.
_SAFE_ID_RE = re.compile(r'^[A-Z0-9]+$')

BASE = "https://www.info-financiere.gouv.fr/api/explore/v2.1/catalog/datasets"
DATASET = "flux-amf-new-prod"

# Opendatasoft caps ``limit`` at 100 per request, so page with ``offset`` (most
# recent first) until ``total_count`` is reached. ODS also caps deep paging at
# offset+limit <= 10000; hitting it is recorded as truncation (never silent).
_PAGE = 100
_MAX_OFFSET = 10_000
_ORDER = "informationdeposee_inf_dat_emt desc"

# Recon-confirmed real fields: the download URL is `url_de_recuperation` (an
# ftp.opendatasoft.com PDF, HTTP 200), NOT a constructed path; the document type is
# `subtype_of_information` (specific) falling back to `type_of_information`.
# Map the English subtype/type labels -> our DOC_TYPES; extend from the data.
_TYPE_MAP = {
    "annual financial report": "annual_report",
    "half-yearly financial report": "half_year_report",
    "interim management statement": "interim_statement",
    "quarterly financial information": "interim_statement",
    "inside information": "inside_information",
    "total number of voting rights and capital": "holding_notification",
    "prospectus": "prospectus",
}


def _doc_type(subtype: str | None, typ: str | None) -> str:
    return _TYPE_MAP.get((subtype or "").strip().lower(),
                         _TYPE_MAP.get((typ or "").strip().lower(), "other"))


# The AMF feed serves machine-readable ESEF annual reports as a report-package (.zip)
# that bundles the report + its extension taxonomy. Tag those "esef" so the EU
# financials Tier B (Arelle) can find and parse them. (Bare .xhtml reports also appear
# on the feed but aren't self-contained — without the bundled taxonomy Arelle resolves
# no facts — so they stay "document".) Everything else is "document".
_ESEF_EXTS = (".zip",)


def _file_kind(url: str) -> str:
    return "esef" if (url or "").lower().split("?")[0].endswith(_ESEF_EXTS) else "document"


class InfoFinanciereFR(OamSource):
    name = "oam-fr"
    country = "FR"

    def discover(self, entity: Entity) -> list[Document]:
        from urllib.parse import quote
        if not entity.lei and not entity.isins:
            return []
        # Build OR-clause over LEI and each ISIN so pre-LEI-era records aren't silently dropped.
        # Validate identifiers against ^[A-Z0-9]+$ before interpolating into the query string
        # to prevent stray quotes from breaking or altering the ODS where-clause.
        clauses = []
        if entity.lei:
            if _SAFE_ID_RE.match(entity.lei):
                clauses.append(f'identificationsociete_iso_cd_lei="{entity.lei}"')
        for isin in entity.isins:
            if _SAFE_ID_RE.match(isin):
                clauses.append(f'identificationsociete_iso_cd_isi="{isin}"')
        if not clauses:
            return []
        where = quote(" OR ".join(f"({c})" if len(clauses) > 1 else c for c in clauses))
        base_q = f"{BASE}/{DATASET}/records?where={where}&order_by={quote(_ORDER)}&limit={_PAGE}"

        # Page through every record (ODS limit is 100/request).
        results: list[dict] = []
        seen_uin: set = set()
        total_count: int | None = None
        offset = 0
        while offset < _MAX_OFFSET:
            q = f"{base_q}&offset={offset}"
            try:
                resp = self.fetcher.get_json(q)
            except Exception as exc:  # noqa: BLE001
                self._record_error("discover", q, exc)
                break
            batch = resp.get("results") or []
            if total_count is None:
                tc = resp.get("total_count")
                if tc is not None:
                    total_count = int(tc)
                # When total_count is absent, pagination is driven by empty pages
                # (mirrors FI/IT pattern) rather than a possibly-missing count.
            for rec in batch:  # defend against any offset overlap
                uin = rec.get("uin_idt_uin")
                if uin not in seen_uin:
                    seen_uin.add(uin)
                    results.append(rec)
            offset += _PAGE
            if not batch or (total_count is not None and len(results) >= total_count):
                break
        else:
            # Loop exhausted by the ODS deep-paging cap with records still missing.
            if total_count and len(results) < total_count:
                self._record_error(
                    "truncated", base_q,
                    f"{len(results)}/{total_count} records (ODS offset cap {_MAX_OFFSET})")
        now = datetime.now(timezone.utc).isoformat()
        out: list[Document] = []
        skipped = 0
        for f in results:
            url = f.get("url_de_recuperation")
            if not url:
                skipped += 1
                continue
            out.append(Document(
                doc_id=f"fr-{f.get('uin_idt_uin')}", lei=entity.lei, country="FR",
                doc_type=_doc_type(f.get("subtype_of_information"),
                                   f.get("type_of_information")),
                period_end=None,  # FR records are publication-dated, not period-keyed
                published_ts=f.get("informationdeposee_inf_dat_emt") or f.get("uin_dat_amf"),
                discovered_ts=now, language="fr", source=self.name,
                files=[{"name": url.rsplit("/", 1)[-1], "url": url, "kind": _file_kind(url)}],
                native_meta=f))
        if skipped:
            self._record_error("no-url", q, f"{skipped} records had no url_de_recuperation")
        return out
