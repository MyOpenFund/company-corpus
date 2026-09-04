"""The canonical source-code registry: one code per authority.

This corpus pulls the same issuer's filings through three pillars — SEC EDGAR,
the EU/OAM network of national storage mechanisms (plus the filings.xbrl.org
ESEF aggregator), and eight national company registers — and, over time, several
different module/class names have tagged rows for the very same authority (e.g.
the EU pillar's backend class is named ``"oam-fr"`` while the concept is "AMF's
info-financiere.gouv.fr mechanism"; a DK register row carries ``source="erst-fsa"``
or ``source="erst-ifrs"`` depending only on *which document* was filed, never on
*which authority* published it).

The rule this module enforces: **one code per real-world regulatory authority**,
never one per module, class, or file-format variant. ``backends`` records which
module/class implements a code (documentation only — the codebase's real
producer/source tags are decoupled from this registry via ``_ALIASES``); the
distinction between "how a filing reached us" (module/class, file format) and
"who is publicly on the hook for it" (the authority) is exactly the
``source_code`` / ``provenance`` split: ``source_code`` is always the authority,
while the finer-grained "how" belongs in the ``provenance`` field of the row —
never smuggled into a second, near-duplicate authority code.

Three pillars:

* ``sec``      — SEC EDGAR (US), 1 code.
* ``oam``      — the EU Officially Appointed Mechanisms + filings.xbrl.org, 14 codes.
* ``register`` — national company registers, 8 codes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CODE_RE = re.compile(r"^[a-z0-9-]{2,16}$")


@dataclass(frozen=True)
class SourceCode:
    """One authority: a stable short code plus its identifying metadata.

    ``backends`` names the module(s)/class(es) that implement acquisition for
    this authority today — documentation of "how", never itself a source of
    truth for identity (that's ``code``).
    """

    code: str
    authority: str
    # The authority's territorial reach, not the issuer's jurisdiction: an ISO
    # country code, or several `/`-joined (e.g. "NL/BE/FR/PT/NO" for Euronext
    # market notices) when one authority covers more than one country.
    country: str
    pillar: str  # "sec" | "oam" | "register"
    id_scheme: str
    backends: tuple[str, ...] = field(default_factory=tuple)


def _sc(code, authority, country, pillar, id_scheme, backends) -> SourceCode:
    return SourceCode(
        code=code, authority=authority, country=country, pillar=pillar,
        id_scheme=id_scheme, backends=tuple(backends),
    )


# ---------------------------------------------------------------------------
# SEC pillar (1)
# ---------------------------------------------------------------------------
_SEC = [
    _sc("sec", "SEC EDGAR", "US", "sec", "cik", ("sec",)),
]

# ---------------------------------------------------------------------------
# EU/OAM pillar (14): the 13 national OAMs + the filings.xbrl.org aggregator.
# Every EU/OAM entity is identified by LEI.
# ---------------------------------------------------------------------------
_OAM = [
    _sc("amf", "AMF (info-financiere.gouv.fr)", "FR", "oam", "lei", ("oam-fr",)),
    _sc("banz", "Bundesanzeiger", "DE", "oam", "lei", ("oam-de",)),
    _sc("consob", "CONSOB 1INFO", "IT", "oam", "lei", ("oam-it",)),
    _sc("cnmv", "CNMV", "ES", "oam", "lei", ("oam-es",)),
    _sc("afm", "AFM", "NL", "oam", "lei", ("oam-nl",)),
    _sc("fsma", "FSMA STORI", "BE", "oam", "lei", ("oam-be",)),
    # GB/IE: the FCA NSM also serves Irish issuers -- eu/acquire.py's
    # COUNTRY_BACKENDS maps "IE" to the same NsmGB backend class.
    _sc("fca", "FCA National Storage Mechanism", "GB/IE", "oam", "lei", ("oam-gb",)),
    _sc("fi-se", "Finansinspektionen (Finanscentralen)", "SE", "oam", "lei", ("oam-se",)),
    _sc("dfsa", "Danish FSA / Finanstilsynet OAM", "DK", "oam", "lei", ("oam-dk",)),
    _sc("oam-fi", "Nasdaq Helsinki (oam.fi)", "FI", "oam", "lei", ("oam-fi",)),
    _sc("newsweb", "Oslo Børs NewsWeb", "NO", "oam", "lei", ("oam-no",)),
    _sc("six", "SIX Swiss Exchange / EQS", "CH", "oam", "lei", ("oam-ch",)),
    _sc(
        "euronext", "Euronext market notices", "NL/BE/FR/PT/NO", "oam", "lei",
        ("euronext",),
    ),
    _sc(
        "xbrlorg", "filings.xbrl.org (ESEF aggregator)", "EU", "oam", "lei",
        ("filings.xbrl.org",),
    ),
]

# ---------------------------------------------------------------------------
# Register pillar (8)
# ---------------------------------------------------------------------------
_REGISTER = [
    _sc(
        "brreg", "Brønnøysund Regnskapsregisteret", "NO", "register", "orgnr",
        ("no_brreg",),
    ),
    _sc(
        "ukch", "UK Companies House", "GB", "register", "companies_house",
        ("ch_bulk", "ch_ixbrl"),
    ),
    _sc(
        "nbb", "National Bank of Belgium (CBSO)", "BE", "register", "kbo",
        ("bnb_cbso", "bnb_xbrl"),
    ),
    _sc(
        "lbr", "Luxembourg Business Registers", "LU", "register", "rcs",
        ("lu_cdb", "lu_ecdf"),
    ),
    _sc(
        "prh", "PRH Finnish Patent & Registration Office", "FI", "register",
        "ytunnus", ("prh_api", "fi_prh_xbrl"),
    ),
    _sc(
        "erst", "Erhvervsstyrelsen / Virk", "DK", "register", "cvr",
        ("virk_api", "dk_fsa_xbrl"),
    ),
    _sc(
        "rik", "Äriregister (RIK avaandmed)", "EE", "register", "registrikood",
        ("ee_csv",),
    ),
    _sc(
        "registeruz", "Register účtovných závierok", "SK", "register", "ico",
        ("sk_registeruz",),
    ),
]

SOURCE_CODES: dict[str, SourceCode] = {
    entry.code: entry for entry in (*_SEC, *_OAM, *_REGISTER)
}

# ---------------------------------------------------------------------------
# Aliases: the strings the rest of the codebase already uses today (backend
# class ``name`` attributes, register ``source`` tags, and a couple of legacy
# spellings) -> the canonical authority code above. A code always maps to
# itself too (handled in ``source_code_for``, not duplicated here).
# ---------------------------------------------------------------------------
_ALIASES: dict[str, str] = {
    # SEC pillar
    "sec": "sec",
    # EU/OAM pillar: backend class `name` attributes.
    "oam-fr": "amf",
    "oam-de": "banz",
    "oam-it": "consob",
    "oam-es": "cnmv",
    "oam-nl": "afm",
    "oam-be": "fsma",
    "oam-gb": "fca",
    "oam-se": "fi-se",
    "oam-dk": "dfsa",
    "oam-fi": "oam-fi",
    "oam-no": "newsweb",
    "oam-ch": "six",
    "euronext": "euronext",
    "filings.xbrl.org": "xbrlorg",
    # The EU facts producer's own `source` tag for filings.xbrl.org-derived rows.
    "esef": "xbrlorg",
    # Register pillar: `_SOURCE_ID_SCHEME` / register `source` tags.
    "brreg": "brreg",
    "companies_house": "ukch",
    "ch": "ukch",
    "bnb": "nbb",
    "lbr": "lbr",
    "prh": "prh",
    "erst-fsa": "erst",
    "erst-ifrs": "erst",
    "rik": "rik",
    "registeruz": "registeruz",
}


def source_code_for(name: str) -> str:
    """Resolve any producer/backend tag (or a canonical code itself) to its
    canonical :data:`SOURCE_CODES` key.

    Raises ``KeyError`` for anything unrecognised — never guesses.
    """
    if name in SOURCE_CODES:
        return name
    return _ALIASES[name]
