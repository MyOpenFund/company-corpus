"""Real SEC responses recorded by ``scripts/record_live_fixtures.py``, and the selection parity
they measure (shared by ``test_positions_parity`` and ``scripts/live_parity_report.py``).

For one issuer, the same facts go through ``run_selection`` twice: with the positions
``filing_positions`` rebuilds from the recorded EDGAR files (live), and with the FSDS rows of the
same accessions (corpus). Only the rows the positions decide are compared: a row whose location
is ``unknown`` on both sides (its filing predates every recorded one, so no position reaches it)
is equal by construction and left out. A compared row is equal when both sides give the same
``(concept, valid_date, frequency, value)``; a row only one side gives counts as different.
"""
from __future__ import annotations

import gzip
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache
from pathlib import Path

import pyarrow as pa

from company_corpus.concepts.selection import run_selection
from company_corpus.config import Config
from company_corpus.live.filings import fetch_submissions, list_filings
from company_corpus.live.positions import filing_positions
from company_corpus.live.sec import SecNotFound
from company_corpus.opendata.batches import issuer_rows
from company_corpus.sources.edgar_xbrl import COMPANYFACTS_URL

RECORDED = Path(__file__).resolve().parent.parent / "fixtures" / "live" / "recorded"
_ROW_KEY = ("concept", "valid_date", "frequency", "basis", "source_code")


@cache
def manifest() -> dict:
    return json.loads((RECORDED / "manifest.json").read_text(encoding="utf-8"))


def recorded_ciks() -> list[str]:
    return sorted(manifest()["issuers"])


def _read(rel: str) -> bytes:
    return gzip.decompress((RECORDED / rel).read_bytes())


class RecordedSec:
    """Serves one issuer's recorded bodies by URL; a URL recorded as a 404, or never recorded,
    is a 404."""

    def __init__(self, cik: str):
        self.files = manifest()["issuers"][cik]["files"]
        self.calls: list[str] = []

    def get_bytes(self, url: str, *, max_age_s: float | None = None) -> bytes:
        self.calls.append(url)
        rel = self.files.get(url)
        if rel is None:
            raise SecNotFound(url)
        return _read(rel)


@dataclass
class Inputs:
    cik: str
    name: str
    facts: pa.Table
    live_positions: list[dict]
    fsds_positions: list[dict]
    live_statements: pa.Table
    fsds_statements: pa.Table
    counters: Counter


#: ``filing_statements`` columns the selection reads, typed so that an issuer without any
#: position still gives a (empty) table.
STATEMENTS_SCHEMA = pa.schema([
    ("entity_scheme", pa.string()), ("entity_value", pa.string()), ("filing_ref", pa.string()),
    ("statement", pa.string()), ("tag", pa.string()), ("tag_version", pa.string()),
    ("plabel", pa.string()), ("valid_date", pa.date32()),
    ("known_at", pa.timestamp("us", tz="UTC")), ("known_at_kind", pa.string()),
    ("source_code", pa.string()), ("source_doc_id", pa.string())])


def _table(rows: list[dict], schema: pa.Schema | None = None) -> pa.Table:
    return pa.Table.from_pylist(rows, schema=schema)


def load_inputs(cik: str) -> Inputs:
    """The selection inputs of one recorded issuer, both position sources."""
    entry = manifest()["issuers"][cik]
    now = datetime.fromisoformat(entry["recorded_at"])
    sec = RecordedSec(cik)
    facts = json.loads(sec.get_bytes(COMPANYFACTS_URL.format(cik=cik)))
    archive = fetch_submissions(cik, sec, max_age_s=None, now=now)
    accns = {a["accn"] for a in entry["accessions"]}
    refs = [r for r in list_filings(archive, forms=manifest()["forms"],
                                    period_from=date.fromisoformat(manifest()["since"]),
                                    period_to=None) if r.accession in accns]
    counters: Counter = Counter()
    live = sorted((r for ref in refs for r in filing_positions(ref, sec, counters=counters)),
                  key=lambda r: (r["accn"], r["statement"], r["tag"]))
    fsds = [json.loads(line) for line in _read(entry["fsds"]).decode("utf-8").splitlines() if line]
    config = Config(data_dir=RECORDED, contact="")
    rows_live = issuer_rows(cik, archive, facts, live, manifest_doc_ids=set(), config=config, now=now)
    rows_fsds = issuer_rows(cik, archive, facts, fsds, manifest_doc_ids=set(), config=config, now=now)
    assert rows_live.fundamentals == rows_fsds.fundamentals
    return Inputs(cik=cik, name=entry["name"], facts=_table(rows_live.fundamentals),
                  live_positions=live, fsds_positions=fsds,
                  live_statements=_table(rows_live.statements, STATEMENTS_SCHEMA),
                  fsds_statements=_table(rows_fsds.statements, STATEMENTS_SCHEMA),
                  counters=counters)


@dataclass
class Difference:
    concept: str
    valid_date: date
    frequency: str
    live: dict | None
    fsds: dict | None
    cause: str

    def __str__(self) -> str:
        def side(r):
            if r is None:
                return "none"
            return f"{r['value']} {r['original_tag']} [{r['location']}, {r['filing_ref']}]"
        return (f"{self.concept} {self.valid_date} {self.frequency}: live {side(self.live)} "
                f"vs FSDS {side(self.fsds)} -- {self.cause}")


@dataclass
class Parity:
    cik: str
    name: str
    equal: int = 0
    borrowed: int = 0
    #: Equal values whose ``location`` or ``on_statement`` differ between the two sides.
    placed_differently: int = 0
    differences: list[Difference] = field(default_factory=list)

    @property
    def compared(self) -> int:
        return self.equal + len(self.differences)

    @property
    def share(self) -> float:
        return self.equal / self.compared if self.compared else 0.0


def _placements(positions: list[dict]) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in positions:
        out[(r["accn"], r["tag"])].add(r["statement"])
    return out


def _cause(live: dict | None, fsds: dict | None, inputs: Inputs) -> str:
    """A guess at why the two sides differ, from where each side places the picked tags."""
    lp, fp = _placements(inputs.live_positions), _placements(inputs.fsds_positions)
    for row in (fsds, live):
        if row is None or ":" not in row["original_tag"]:
            continue
        key = (row["filing_ref"], row["original_tag"].split(":", 1)[1].split("+")[0])
        on_live, on_fsds = lp.get(key, set()), fp.get(key, set())
        if on_live != on_fsds:
            return (f"{key[1]} in {key[0]}: live statements {sorted(on_live) or 'none'}, "
                    f"FSDS {sorted(on_fsds) or 'none'}")
    if live is None or fsds is None:
        return "row on one side only"
    if live["location"] != fsds["location"]:
        return f"location {live['location']} vs {fsds['location']}"
    return "same placements of the picked tags: a competing tag is placed differently"


def compare(inputs: Inputs) -> Parity:
    live = run_selection(inputs.facts, inputs.live_statements)
    fsds = run_selection(inputs.facts, inputs.fsds_statements)
    by_key = lambda rows: {tuple(r[k] for k in _ROW_KEY): r for r in rows}  # noqa: E731
    lk, fk = by_key(live), by_key(fsds)
    assert len(lk) == len(live) and len(fk) == len(fsds), "selection keys are not unique"
    out = Parity(inputs.cik, inputs.name)
    for key in sorted(set(lk) | set(fk), key=str):
        a, b = lk.get(key), fk.get(key)
        if all(r is None or r["location"] == "unknown" for r in (a, b)):
            continue
        if a is not None and b is not None and a["value"] == b["value"]:
            out.equal += 1
            out.borrowed += a["location"] == "inferred"
            out.placed_differently += (a["location"], a["on_statement"]) != \
                (b["location"], b["on_statement"])
        else:
            out.differences.append(Difference(key[0], key[1], key[2], a, b, _cause(a, b, inputs)))
    return out
