from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date

DOC_TYPES = ("annual_report", "half_year_report", "interim_statement",
             "inside_information", "holding_notification", "prospectus",
             "governance", "other")

DOC_FAMILY = {"annual_report": "ESEF-AR", "half_year_report": "HY",
              "interim_statement": "IMS", "inside_information": "MAR",
              "holding_notification": "TVR", "prospectus": "PROSPECTUS",
              "governance": "GOV", "other": "OTHER"}


@dataclass
class Document:
    doc_id: str
    lei: str | None
    country: str
    doc_type: str
    period_end: date | None
    published_ts: str | None
    discovered_ts: str
    language: str | None
    source: str
    files: list[dict] = field(default_factory=list)
    native_meta: dict = field(default_factory=dict)

    def key(self) -> tuple:
        hashes = tuple(sorted(f.get("sha256") or f.get("name", "") for f in self.files))
        return (self.lei, self.doc_type, self.period_end, hashes)
