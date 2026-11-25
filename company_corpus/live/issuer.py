"""One issuer's four store tables, computed live from SEC responses (no corpus, no store).

The rows come from the producer's own row functions through ``opendata.batches.issuer_rows``,
and the tables from the open-data-store contract schemas through ``contract_table``, exactly as
``store-batches`` builds them: the selection gives the same numbers on both. No corpus manifest
exists here, so ``manifest_doc_ids`` is empty and every row counts ``filing_not_in_manifest``
(kept in ``counters``, inert).

Statement positions are rebuilt per filing (``filing_positions``) for the report forms of
``POSITIONS_FORMS`` whose ``report_date`` falls in the window, and ordered like the FSDS file
``statements`` writes. A filing whose files stay unreachable (``SecUnavailable``) or vanish
(``SecNotFound``) counts ``positions:fetch_failed`` and has no positions of its own: the selection
borrows those of an earlier filing. ``SecRefused`` and ``SecContactMissing`` concern every
request and propagate.
"""
from __future__ import annotations

import json
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime

import pyarrow as pa

from ..config import Config, normalize_cik
from ..opendata.batches import contract_table, contracts_api, issuer_rows
from ..sources.edgar_xbrl import COMPANYFACTS_URL
from .filings import FilingRef, fetch_submissions, list_filings
from .positions import filing_positions
from .sec import SecGet, SecNotFound, SecUnavailable

#: Forms whose statement positions are rebuilt (their ``/A`` amendments included).
POSITIONS_FORMS: tuple[str, ...] = ("10-K", "10-Q", "20-F", "40-F")


@dataclass
class IssuerTables:
    cik: str
    entity_observations: pa.Table
    fundamentals: pa.Table
    filings: pa.Table
    filing_statements: pa.Table
    latest_filing: FilingRef | None
    counters: Counter


def _positions(archive, sec: SecGet, *, forms: Collection[str], period_from: date | None,
               period_to: date | None, counters: Counter) -> list[dict]:
    rows: list[dict] = []
    for ref in list_filings(archive, forms=forms, period_from=period_from, period_to=period_to):
        try:
            rows.extend(filing_positions(ref, sec, counters=counters))
        except (SecUnavailable, SecNotFound):
            counters["positions:fetch_failed"] += 1
    rows.sort(key=lambda r: (r["accn"], r["statement"], r["tag"]))  # the order ``_extract`` writes
    return rows


def issuer_tables(cik: str, sec: SecGet, *, config: Config, now: datetime,
                  positions_from: date | None, positions_to: date | None,
                  facts_max_age_s: float | None, submissions_max_age_s: float | None,
                  positions_forms: Collection[str] = POSITIONS_FORMS) -> IssuerTables:
    """Fetch companyfacts, submissions and the window's statement positions of ``cik`` and build
    its ``entity_observations``, ``fundamentals``, ``filings`` and ``filing_statements`` tables.
    A companyfacts 404 raises ``SecNotFound`` (the issuer has no XBRL facts)."""
    mod = contracts_api(needed_by="live mode", extra="live")
    cik = normalize_cik(cik)
    facts = json.loads(sec.get_bytes(COMPANYFACTS_URL.format(cik=cik), max_age_s=facts_max_age_s))
    archive = fetch_submissions(cik, sec, max_age_s=submissions_max_age_s, now=now)
    counters: Counter = Counter()
    statements = _positions(archive, sec, forms=positions_forms, period_from=positions_from,
                            period_to=positions_to, counters=counters)
    rows = issuer_rows(cik, archive, facts, statements, manifest_doc_ids=set(), config=config,
                       now=now)
    counters.update(rows.counters)
    newest = list_filings(archive, forms=None, period_from=None, period_to=None)
    t = {contract.table_name: contract_table(mod, contract, contract_rows)
         for contract, contract_rows in rows.by_contract(mod)}
    return IssuerTables(
        cik=cik,
        entity_observations=t[mod.ENTITY_OBSERVATIONS.table_name],
        fundamentals=t[mod.FUNDAMENTALS.table_name],
        filings=t[mod.FILINGS.table_name],
        filing_statements=t[mod.FILING_STATEMENTS.table_name],
        latest_filing=newest[0] if newest else None,
        counters=counters)
