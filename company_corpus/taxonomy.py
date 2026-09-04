"""Filing-type taxonomy for the bottom-up (company) corpus.

Mirrors the family/letter scheme of cb_corpus (which classifies central-bank
documents A-G). Here the families group SEC EDGAR form types into the
primary-source company-disclosure families that matter for downstream RAG.

Each :class:`FormType` carries a stable code (``A1``...), the family letter, a
human label, and the set of raw EDGAR form strings it maps to. ``FULL_SCOPE``
is the default crawl scope (narrative families A-D); ownership (E) and the
structured-financials pseudo-family (F) are opt-in.
"""

from __future__ import annotations

from enum import Enum


class FormType(Enum):
    """Company-disclosure families, analog of cb_corpus ``DocType``.

    Value tuple: ``(code, family, label, edgar_forms)`` where ``edgar_forms`` is
    a tuple of raw EDGAR form strings (as they appear in EDGAR indices/filings)
    that map to this family.
    """

    # A. Periodic reports
    A1 = ("A1", "A", "Annual report (10-K)", ("10-K", "10-KSB", "10-K405"))
    A2 = ("A2", "A", "Quarterly report (10-Q)", ("10-Q", "10-QSB"))
    A3 = ("A3", "A", "Foreign annual report (20-F)", ("20-F",))
    A4 = ("A4", "A", "Canadian annual report (40-F)", ("40-F",))

    # B. Current / material events (incl. earnings-release exhibits)
    B1 = ("B1", "B", "Current report (8-K)", ("8-K",))
    B2 = ("B2", "B", "Foreign current report (6-K)", ("6-K",))

    # C. Governance
    C1 = ("C1", "C", "Definitive proxy statement (DEF 14A)", ("DEF 14A",))
    C2 = ("C2", "C", "Other proxy material", ("DEFA14A", "PRE 14A", "DEFM14A"))

    # D. Registration / offering
    D1 = ("D1", "D", "Registration statement (S-1)", ("S-1", "S-1/A"))
    D2 = ("D2", "D", "Business-combination registration (S-4)", ("S-4", "S-4/A"))
    D3 = ("D3", "D", "Prospectus (424B)", ("424B1", "424B2", "424B3", "424B4", "424B5"))

    # E. Ownership (structured) -- opt-in
    E1 = ("E1", "E", "Insider transactions (Forms 3/4/5)", ("3", "4", "5"))
    E2 = ("E2", "E", "Institutional holdings (13F)", ("13F-HR", "13F-NT"))
    E3 = ("E3", "E", "Beneficial ownership (SC 13D/G)", ("SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A"))

    # F. Structured financials -- pseudo-family (XBRL, not a single filing) -- opt-in
    F1 = ("F1", "F", "XBRL company facts / financial statement datasets", ())

    def __init__(self, code: str, family: str, label: str, edgar_forms: tuple[str, ...]):
        self.code = code
        self.family = family
        self.label = label
        self.edgar_forms = edgar_forms

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


# Families crawled by default (narrative primary sources). Ownership (E) and the
# structured-financials pseudo-family (F) are opt-in, mirroring cb_corpus
# excluding its family G by default.
FULL_SCOPE_FAMILIES = ("A", "B", "C", "D")
FULL_SCOPE: tuple[FormType, ...] = tuple(
    ft for ft in FormType if ft.family in FULL_SCOPE_FAMILIES
)

# Reverse lookup: raw EDGAR form string -> FormType.
_EDGAR_FORM_INDEX: dict[str, FormType] = {
    form: ft for ft in FormType for form in ft.edgar_forms
}


def by_code(code: str) -> FormType:
    """Return the :class:`FormType` for a taxonomy code (e.g. ``"A1"``)."""
    try:
        return FormType[code.strip().upper()]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"unknown FormType code: {code!r}") from exc


def from_edgar_form(edgar_form: str) -> FormType | None:
    """Map a raw EDGAR form string (e.g. ``"10-K"``) to a :class:`FormType`.

    Returns ``None`` when the form is outside the taxonomy. Matching is
    case-insensitive and tolerant of surrounding whitespace.
    """
    return _EDGAR_FORM_INDEX.get(edgar_form.strip().upper())


def parse_scope(codes: str | None) -> tuple[FormType, ...]:
    """Parse a comma-separated code/family selector into FormTypes.

    Accepts taxonomy codes (``"A1,B1"``), family letters (``"A,B"``), or the
    keyword ``"all"``. ``None`` / empty -> :data:`FULL_SCOPE`.
    """
    if not codes or codes.strip().lower() in {"", "default"}:
        return FULL_SCOPE
    if codes.strip().lower() == "all":
        return tuple(FormType)
    selected: list[FormType] = []
    for token in codes.split(","):
        token = token.strip()
        if not token:
            continue
        if len(token) == 1 and token.upper().isalpha():
            selected.extend(ft for ft in FormType if ft.family == token.upper())
        else:
            selected.append(by_code(token))
    # De-duplicate while preserving order.
    seen: set[FormType] = set()
    ordered: list[FormType] = []
    for ft in selected:
        if ft not in seen:
            seen.add(ft)
            ordered.append(ft)
    return tuple(ordered)
