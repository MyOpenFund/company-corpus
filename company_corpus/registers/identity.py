"""Resolve register specs to canonical entity keys:
- Norway:      orgnr directly, or LEI -> GLEIF registeredAs -> orgnr (9 digits).
- UK:          ch_number directly (8 alphanumerics), or LEI -> GLEIF registeredAs ->
               ch_number (only when legalAddress.country == "GB").
- Belgium:     be_number directly (KBO, 10 digits), or LEI -> GLEIF registeredAs ->
               be_number (only when legalAddress.country == "BE").
- Finland:     business_id directly (Y-tunnus NNNNNNN-N), or LEI -> GLEIF registeredAs ->
               business_id (only when legalAddress.country == "FI").
- Luxembourg:  rcs directly, or LEI -> GLEIF registeredAs -> rcs
               (only when legalAddress.country == "LU").
- Denmark:     cvr directly (8-digit string), or LEI -> GLEIF registeredAs -> cvr
               (only when legalAddress.country == "DK").
- Estonia:     registrikood directly (8 digits), or LEI -> GLEIF registeredAs ->
               registrikood (only when legalAddress.country == "EE").
- Slovakia:    ico directly (8 digits), or LEI -> GLEIF registeredAs -> ico
               (only when legalAddress.country == "SK").

Every ``_norm_*`` helper VALIDATES and returns ``str | None``; none of them
repairs. They used to digit-strip and zero-pad whatever they were handed, so
``"N/A"`` -- which GLEIF really does carry in ``registeredAs`` -- became
``"0000000000"``: truthy, emitted with ``status: "ok"``, and used verbatim as a
table filename, so every degenerate entity in a run wrote to the same file and
erased the previous one (DI-I4 / DI-M4). A ``None`` is routed to the
``unresolved`` branch with the offending value recorded, on the direct-spec and
the GLEIF paths alike: a spec that names an identifier we cannot read is a fact
worth reporting, never a guess worth making.
"""
from __future__ import annotations
import re

from ..gleif import fetch_gleif_record, parse_gleif_record

# Register-published identifier formats. These are dictated by each register,
# not chosen by us, so they are constants with their authority named:
# Brreg orgnr 9 digits; KBO/BCE 10 digits (9-digit legacy form left-padded);
# PRH Y-tunnus NNNNNNN-N; LBR RCS a letter + up to 6 digits; Erhvervsstyrelsen
# CVR 8 digits; RIK registrikood 8 digits; RegisterUZ ICO 8 digits; Companies
# House 8 alphanumerics.
_ORGNR_RE = re.compile(r"\A\d{9}\Z")
_YTUNNUS_RE = re.compile(r"\A\d{7}-\d\Z")
_RCS_RE = re.compile(r"\A[A-Z]\d{1,6}\Z")
_CH_RE = re.compile(r"\A[A-Z0-9]{8}\Z")


def _has_a_nonzero_digit(text: str) -> bool:
    """True when ``text`` carries at least one digit that is not ``0``.

    An all-zero identifier is never a real entity in any of these registers; it
    is what a feed writes where an identifier belonged. Padding one into shape
    minted a plausible key that every degenerate row in a run then shared.
    """
    return any(ch.isdigit() and ch != "0" for ch in text)


def _digits_id(value, *, lengths: tuple[int, ...], pad_to: int) -> "str | None":
    """Digit-only identifier of one of ``lengths``, left-padded to ``pad_to``.

    Returns ``None`` -- never a padded guess -- when the digit count is not one
    the register publishes, or when every digit is a zero.
    """
    digits = re.sub(r"\D", "", str(value))
    if len(digits) not in lengths or not _has_a_nonzero_digit(digits):
        return None
    return digits.zfill(pad_to)


def _norm_ch_number(s) -> "str | None":
    """Companies House number: 8 alphanumerics; a pure-digit form is padded.

    ``"510976"`` -> ``"00510976"``, ``"SC741022"`` verbatim. Anything that is
    not 8 characters after padding (prose, ``"N/A"``, a truncated id) is None.
    """
    text = str(s).strip().upper()
    if text.isdigit():
        text = text.zfill(8)
    if not _CH_RE.match(text) or not _has_a_nonzero_digit(text):
        return None
    return text


def _norm_orgnr(s) -> "str | None":
    """Brreg organisasjonsnummer: exactly 9 digits, never padded into shape.

    Replaces the two inline ``"".join(ch for ch in str(x) if ch.isdigit())``
    digit-strips (the NO direct-spec and GLEIF paths) which happily produced
    ``""`` and emitted it with ``status: "ok"``.
    """
    return _digits_id(s, lengths=(9,), pad_to=9)


def _norm_kbo(s) -> "str | None":
    """KBO/BCE number: 10 digits (a 9-digit legacy form is left-padded); else None.

    It used to digit-strip then ``zfill(10)``, so ``"N/A"`` -- which GLEIF really
    does carry in ``registeredAs`` -- became ``"0000000000"``: truthy, emitted
    with ``status: "ok"``, and used verbatim as a table filename, so every
    degenerate entity in a run wrote to the same file and erased the previous
    one (DI-I4 / DI-M4).
    """
    return _digits_id(s, lengths=(9, 10), pad_to=10)


def _norm_ytunnus(s) -> "str | None":
    """PRH Y-tunnus: ``NNNNNNN-N`` exactly, whitespace stripped; else None.

    It used to be a bare ``.strip()``, so any prose in ``registeredAs`` was
    handed to the PRH client as a business id and then written as a filename.
    """
    text = re.sub(r"\s+", "", str(s))
    if not _YTUNNUS_RE.match(text) or not _has_a_nonzero_digit(text):
        return None
    return text


def _norm_rcs(s) -> "str | None":
    """LBR RCS number: a register letter + 1-6 digits (``"B 60814"`` -> ``"B60814"``).

    Spaces and dots are removed and the letter upper-cased; digits are never
    zero-padded. It used to be the strip alone, so ``"../../pwn"`` survived to
    become a table path (Rob-C7).
    """
    text = re.sub(r"[\s.]+", "", str(s)).upper()
    if not _RCS_RE.match(text) or not _has_a_nonzero_digit(text):
        return None
    return text


def _norm_cvr(s) -> "str | None":
    """Erhvervsstyrelsen CVR: exactly 8 digits, leading zeros preserved; else None.

    It used to return whatever was left after stripping whitespace and "let the
    caller's fetch fail safe" -- but the caller writes the table before anything
    fails, so the unusable value had already become a filename.
    """
    return _digits_id(s, lengths=(8,), pad_to=8)


def _norm_registrikood(s) -> "str | None":
    """RIK registrikood: exactly 8 digits; else None (never padded into shape)."""
    return _digits_id(s, lengths=(8,), pad_to=8)


def _norm_ico(s) -> "str | None":
    """RegisterUZ IČO: exactly 8 digits; else None (never padded into shape)."""
    return _digits_id(s, lengths=(8,), pad_to=8)


#: ``spec`` key -> (country, validator). Order is the direct-path precedence.
_DIRECT_PATHS: tuple[tuple[str, str, object], ...] = (
    ("ch_number", "GB", _norm_ch_number),
    ("orgnr", "NO", _norm_orgnr),
    ("be_number", "BE", _norm_kbo),
    ("business_id", "FI", _norm_ytunnus),
    ("rcs", "LU", _norm_rcs),
    ("cvr", "DK", _norm_cvr),
    ("registrikood", "EE", _norm_registrikood),
    ("ico", "SK", _norm_ico),
)

#: GLEIF ``legalAddress.country`` -> (result key, validator). A country we do not
#: cover, or a ``registeredAs`` the validator rejects, stays unresolved: GLEIF's
#: ``registeredAs`` is free text and really does carry ``"N/A"`` and prose.
_GLEIF_PATHS: dict[str, tuple[str, object]] = {
    "NO": ("orgnr", _norm_orgnr),
    "GB": ("ch_number", _norm_ch_number),
    "BE": ("be_number", _norm_kbo),
    "FI": ("business_id", _norm_ytunnus),
    "LU": ("rcs", _norm_rcs),
    "DK": ("cvr", _norm_cvr),
    "EE": ("registrikood", _norm_registrikood),
    "SK": ("ico", _norm_ico),
}


def _unresolved(spec: dict, *, lei, name, country: str, error: "str | None" = None) -> dict:
    """The one unresolved row shape. ``orgnr: None`` is kept for compatibility
    with the NO consumer, which reads ``r.get("orgnr")``; every other consumer
    reads its own key, which is absent (hence falsy) here. ``error`` names the
    value we refused, so an unreadable identifier is reported, not silent."""
    row = {"orgnr": None, "lei": lei, "name": name or spec.get("name", ""),
           "country": country, "status": "unresolved"}
    if error:
        row["error"] = error
    return row


def resolve_register_specs(specs: list[dict], *, fetcher) -> list[dict]:
    out: list[dict] = []
    for spec in specs:
        # --- Direct paths: the caller named a national identifier ---
        for key, country, norm in _DIRECT_PATHS:
            raw = spec.get(key)
            if not raw:
                continue
            value = norm(raw)
            if value is None:
                # A named-but-unreadable identifier is a recorded refusal, not a
                # fall-through to GLEIF: guessing a second identity for a spec
                # whose first one was junk is exactly the no-guess rule.
                out.append(_unresolved(spec, lei=spec.get("lei"), name=None,
                                       country=country,
                                       error=f"not a usable {key}: {raw!r}"))
            else:
                out.append({key: value, "lei": spec.get("lei"),
                            "name": spec.get("name", ""),
                            "country": country, "status": "ok"})
            break
        else:
            # --- LEI -> GLEIF path (NO, GB, BE, FI, LU, DK, EE, SK) ---
            lei = spec.get("lei")
            key = value = name = None
            country = ""
            error = None
            if lei:
                # GLEIF failure / unknown LEI -> record is None -> empty fields ->
                # falls through to the "unresolved" branch below (never guesses).
                record = fetch_gleif_record(lei, fetcher=fetcher)
                fields = parse_gleif_record((record or {}).get("attributes", {}))
                country = fields["legal_country"] or ""
                name = fields["name"]
                ra = fields["registered_as"]
                key, norm = _GLEIF_PATHS.get(country, (None, None))
                if key and ra:
                    value = norm(ra)
                    if value is None:
                        error = f"GLEIF registeredAs is not a usable {key}: {ra!r}"
            if value:
                out.append({key: value, "lei": lei, "name": name or spec.get("name", ""),
                            "country": country, "status": "ok"})
            else:
                out.append(_unresolved(spec, lei=lei, name=name, country=country,
                                       error=error))
    return out
