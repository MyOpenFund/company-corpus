"""Runtime configuration for the bottom-up corpus builder.

Parallels ``cb_corpus.config.Config``. The defaults bake in SEC EDGAR
fair-access compliance: a declared ``User-Agent`` carrying a contact address and
a per-host request rate at or below the SEC's published limit of 10 requests per
second.

Set ``contact`` (or the ``COMPANY_CORPUS_CONTACT`` env var) before any live
crawl -- the SEC asks for a real contact in the User-Agent. There is no default
contact: if neither is set, the User-Agent carries only the tool name and no
email address is sent.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# SEC fair-access program: no more than 10 requests/second per requester.
# We default to a comfortable margin under that ceiling.
SEC_MAX_REQUESTS_PER_SECOND = 10.0
_DEFAULT_RPS = 8.0


def _default_contact() -> str:
    # No hardcoded default: an unset env var means no contact is sent at all.
    return os.environ.get("COMPANY_CORPUS_CONTACT", "")


#: Manifest fields only the download and render phases can produce. A discovery
#: record leaves them empty by construction, so the manifest merge carries the
#: stored value forward instead of letting a second ``discover --write`` erase
#: every download pointer and hash the corpus already had (DI-C1). Not a
#: generic "carry forward anything empty": a name legitimately corrected to ""
#: or a withdrawn ``period_of_report`` must still be clearable.
STICKY_MANIFEST_FIELDS: tuple[str, ...] = (
    "local_path", "sha256", "primary_path", "text_path", "pdf_path",
)


@dataclass
class Config:
    """Paths, networking, and politeness knobs for a corpus run."""

    data_dir: Path = Path("./data")
    contact: str = field(default_factory=_default_contact)

    # Networking / fair access.
    requests_per_second: float = _DEFAULT_RPS
    timeout: float = 30.0          # connect/read inactivity timeout
    download_timeout: float = 120.0  # hard deadline for a single body download
    max_retries: int = 3           # retries with exponential backoff (2**n s)
    verify_tls: bool = True        # disable only behind a trusted SSL-inspection proxy

    # Storage behaviour.
    store_full_submission: bool = True  # keep the complete-submission .txt
    store_primary_doc: bool = True      # decompose + keep the primary document
    store_clean_text: bool = True       # extract RAG-ready plaintext

    # Convergence behaviour (chantier 3).
    sticky_manifest_fields: tuple[str, ...] = STICKY_MANIFEST_FIELDS
    replace_tables: bool = False      # True = a run's rows replace the table wholesale
    no_shrink_fraction: float = 0.0   # largest fraction of a table's groups a write may drop
    lock_wait_seconds: float = 0.0    # 0.0 = a second writer fails immediately
    max_path_component_length: int = 128  # longest real identifier is a 20-char LEI

    def __post_init__(self) -> None:
        if isinstance(self.data_dir, str):
            self.data_dir = Path(self.data_dir)
        if self.requests_per_second > SEC_MAX_REQUESTS_PER_SECOND:
            raise ValueError(
                f"requests_per_second={self.requests_per_second} exceeds the SEC "
                f"limit of {SEC_MAX_REQUESTS_PER_SECOND}/s"
            )

    @property
    def user_agent(self) -> str:
        """SEC-compliant User-Agent string.

        Carries a contact address when one is configured; with no contact set
        it falls back to the bare tool name so we never broadcast a default
        email address.
        """
        if self.contact:
            return f"company-corpus/0.1 ({self.contact})"
        return "company-corpus/0.1"

    @property
    def min_delay_seconds(self) -> float:
        """Minimum spacing between requests to the same host."""
        return 1.0 / self.requests_per_second if self.requests_per_second else 0.0

    # ---- derived paths (mirror cb_corpus layout) ----
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def manifest_dir(self) -> Path:
        return self.data_dir / "manifest"

    @property
    def universe_dir(self) -> Path:
        return self.data_dir / "universe"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def financials_dir(self) -> Path:
        return self.data_dir / "financials"

    @property
    def financials_eu_dir(self) -> Path:
        return self.data_dir / "financials_eu"

    @property
    def financials_register_dir(self) -> Path:
        return self.data_dir / "financials_register"

    @property
    def ownership_dir(self) -> Path:
        return self.data_dir / "ownership"

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"

    @property
    def cik_lookup_path(self) -> Path:
        return self.reference_dir / "cik-lookup-data.txt"

    @property
    def name_cache_path(self) -> Path:
        return self.reference_dir / "name_cik_cache.csv"

    @property
    def discovery_errors_path(self) -> Path:
        return self.data_dir / "discovery_errors.jsonl"

    def manifest_file(self, cik: str) -> Path:
        """Per-issuer manifest path: ``data/manifest/<zero-padded-cik>.jsonl``."""
        return self.manifest_dir / f"{normalize_cik(cik)}.jsonl"


def normalize_cik(cik: str | int) -> str:
    """Return a CIK as a zero-padded 10-digit string (EDGAR canonical form).

    Tolerates inputs like ``320193``, ``"0000320193"``, or ``"CIK0000320193"``.

    An all-zero input is REFUSED rather than padded. EDGAR assigns no CIK 0, so
    ``"0"`` / ``"0000000000"`` is always a placeholder a feed put where an
    identifier belonged; padding it minted one plausible key that every
    degenerate row in a run then shared, so they overwrote each other's manifest
    and financials table (DI-I4 / DI-M4).
    """
    digits = "".join(ch for ch in str(cik) if ch.isdigit())
    if not digits or not digits.strip("0"):
        raise ValueError(f"not a valid CIK: {cik!r}")
    return digits.zfill(10)


#: ISO-17442: a LEI is exactly 20 upper-case alphanumerics.
LEI_RE = re.compile(r"\A[A-Z0-9]{20}\Z")


def normalize_lei(lei: str) -> str:
    """Return a LEI upper-cased and trimmed; validate the ISO-17442 shape.

    The EU financials writer used to pass the caller's raw LEI straight through
    as a filename while the SEC writer normalised its CIK, so a lower-case LEI
    in a spec file split one issuer's periods across two files on a
    case-sensitive filesystem, and recorded a path that does not exist as
    spelled on a case-folding one (DI-I7).

    The SHAPE is checked, not the ISO 7064 mod-97-10 check digits: whether a
    well-formed LEI exists is GLEIF's authority, not this function's, and the
    canonical spelling is all a path component needs.
    """
    value = str(lei).strip().upper()
    if not LEI_RE.match(value):
        raise ValueError(f"not a valid LEI: {lei!r}")
    return value


def normalize_cusip(cusip: str) -> str:
    """Return a CUSIP upper-cased and trimmed; validate length (8 or 9) + charset.

    A full CUSIP is 9 alphanumeric characters (issuer 6 + issue 2 + check 1); the
    8-character form (no check digit) also occurs in feeds and is accepted.
    """
    value = str(cusip).strip().upper()
    if len(value) not in (8, 9) or not value.isalnum():
        raise ValueError(f"not a valid CUSIP: {cusip!r}")
    return value


def cusip6(value: str) -> str:
    """Return the 6-character issuer prefix (CUSIP6) from a CUSIP or US ISIN.

    A US ISIN is ``"US"`` + the 9-char CUSIP + a check digit (12 chars total);
    its embedded CUSIP6 is characters 2..8. Anything else is treated as a CUSIP
    and truncated to its first 6 characters.
    """
    s = str(value).strip().upper()
    if len(s) == 12 and s[:2].isalpha():
        return s[2:8]
    return s[:6]


def cusip_full(value: str) -> str:
    """Return the full CUSIP (8-9 chars) from a CUSIP or US ISIN; ``""`` if neither.

    A US ISIN is ``"US"`` + the 9-char CUSIP + a check digit; its embedded CUSIP is
    characters 2..11. A bare 8/9-char alphanumeric token is returned as-is.
    """
    s = str(value).strip().upper()
    if len(s) == 12 and s[:2].isalpha():
        return s[2:11]
    if len(s) in (8, 9) and s.isalnum():
        return s
    return ""
