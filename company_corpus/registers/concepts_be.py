"""Map Belgium BNB CBSO dimensional-XBRL facts to our curated concepts.

Consumes the flat fact list produced by
:func:`company_corpus.registers.bnb_xbrl.parse_bnb_data_xbrl` — each item
``{"dims": {dim_local: member_local}, "value": float, "unit": str}`` — and
produces the same shape as the CH/UK sibling
(:func:`company_corpus.registers.concepts_uk.map_ch_facts`):
``{period_end, basis, currency, values, suppressed, unbalanced}``, where
``values[key] = {"value", "unit", "label", "tag"}``.

GOVERNING PRINCIPLE — NO FALSE DATA. This corpus is open data for the credit
universe: a number we cannot confirm must never be emitted; a *missing* number
is strictly better than a *wrong* one. So we suppress anything ambiguous and
record the reason.

The BNB instance is *dimensional* (EBA/DPM-style): every monetary fact is a
generic metric qualified by its context's dimension members. Meaning lives in
the members — ``bas`` (rubric: m25 balance-sheet total, m37 equity, m50 total
liabilities, m51 financial borrowings, m53 turnover, m59 net result…), ``part``
(m1 assets, m3 equity&liabilities, m4 income statement), ``prd`` (m1 current,
m2 prior). Other dims (``ntr`` nature, ``rst`` maturity, ``typ``, ``sts``,
``spec``, ``mdp``…) *disaggregate* a total. So the dimensional risk is picking a
disaggregated fact instead of the total: e.g. ``m59/m4 spec=m16`` is *pre-tax*
result (54.4M) while the breakdown-free ``m59/m4`` is the real net result
(115.9M). Every curated value must be the unambiguous total, or suppressed.

Canonical-member selection. For a key ``(bas, part, required-members)`` we take
the fact whose ``dims == {bas, part, prd:m1} ∪ required`` and carries **no other
dimension** (breakdown-free), with a EUR unit. If 0 or >1 such fact exists the
total is not unambiguously identifiable -> suppress the key.

The confidence gate (§4 of the design doc):
- **Primary:** total assets (``m25/m1``) must equal the total passif
  (``m25/m3``) — both independently tagged — within tolerance. Mismatch -> the
  whole filing is untrustworthy: ``unbalanced=True``, no values.
- **Financial debt** (real borrowings — the rich payoff): the ``m51`` tranche
  sum, emitted only when an INDEPENDENT witness reconciles (the financial-nature
  slice of total liabilities, ``m50[ntr=m3]`` — a *different* rubric). If they
  do not reconcile, or the tranche structure deviates, the borrowings figure is
  unconfirmable -> suppress the block (fall back to liabilities-based leverage
  rather than emit a possibly-wrong number). Emitted ATOMICALLY (both halves).
- ``operating_profit`` (m44) is always suppressed — label ambiguous on the one
  validated real filing, pending a 2nd real example.
"""
from __future__ import annotations


from ._common import _CURRENCY_RE, _tol

# Curated key -> (bas, part, required-members). The canonical fact for a key has
# ``dims == {"bas":bas, "part":part, "prd":"m1"} ∪ required`` and NO other
# dimension. Every entry below is validated to ±1 EUR on the real m02 fixture.
# Mandatory structural members (not breakdowns): equity ntr=m4, P&L ntr=m6,
# tax spec=m17, depreciation mdp=m1; inventory/receivables carry the maturity/
# status member that names the on-balance-sheet total.
BE_PACK: dict[str, tuple[str, str, dict[str, str]]] = {
    "assets":              ("m25", "m1", {}),
    "non_current_assets":  ("m2",  "m1", {}),   # canonical key (was assets_fixed)
    "assets_current":      ("m12", "m1", {}),
    "cash":                ("m23", "m1", {}),
    "inventory":           ("m14", "m1", {"sts": "m2"}),
    "receivables":         ("m9",  "m1", {"rst": "m2"}),   # ST receivables
    "equity":              ("m37", "m3", {"ntr": "m4"}),
    "provisions":          ("m47", "m3", {}),
    "liabilities":         ("m50", "m3", {}),
    "liabilities_current": ("m50", "m3", {"rst": "m2"}),
    "revenue":             ("m53", "m4", {"ntr": "m6"}),
    "net_income":          ("m59", "m4", {}),
    "income_tax":          ("m60", "m4", {"spec": "m17"}),
    "dep_amort":           ("m2",  "m4", {"ntr": "m6", "mdp": "m1"}),
}

# Concepts we never emit, with the reason recorded in ``suppressed``.
_ALWAYS_SUPPRESS: dict[str, str] = {
    "operating_profit": "label ambiguous — pending 2nd real example",
}

# The exact dimension set of a valid balance-sheet financial-borrowings tranche:
# a maturity bucket (rst) split by instrument type (typ). Anything else under
# ``m51/part=m3/ntr=m3`` is a deviating structure we cannot cleanly sum.
_TRANCHE_DIMS = {"bas", "ntr", "part", "prd", "rst", "typ"}


def _currency(flat: list[dict]) -> str:
    """The reporting currency — the first ISO-4217 unit seen, else ``"EUR"``."""
    for fact in flat:
        unit = fact.get("unit", "")
        if _CURRENCY_RE.match(unit):
            return unit
    return "EUR"


def _select(flat: list[dict], bas: str, part: str, required: dict[str, str]):
    """The breakdown-free canonical value for ``(bas, part, required)``, or None.

    Returns the value of the *unique* EUR fact whose ``dims`` equals
    ``{bas, part, prd:m1} ∪ required`` exactly (no other dimension). If 0 or >1
    such fact exists the total is not unambiguously identifiable -> None."""
    want = {"bas": bas, "part": part, "prd": "m1", **required}
    hits = [f["value"] for f in flat
            if f.get("unit") == "EUR" and f.get("dims") == want]
    return hits[0] if len(hits) == 1 else None


def _tag(bas: str, part: str, required: dict[str, str]) -> str:
    members = " ".join(f"{k}={v}" for k, v in sorted(required.items()))
    return f"{bas}/{part}" + (f" {members}" if members else "")


def _emit_financial_debt(flat: list[dict], emit, suppressed: list) -> None:
    """Emit ``long_term_debt``/``short_term_debt`` (real m51 borrowings), or
    suppress the block atomically with a recorded reason.

    ``LT`` = Σ balance-sheet borrowings maturing >1yr (``bas=m51, ntr=m3,
    part=m3``) in the ``rst=m1`` maturity bucket, each carrying a ``typ``
    instrument tranche; ``ST`` = the same for ``rst=m2``. The subordinated
    cross-cut (``sts``) is excluded (it double-counts across the tranches).

    NO-FALSE-DATA guard — an INDEPENDENT per-bucket cross-check, not trust in
    the tranche logic: **both** ``LT`` and ``ST`` must each reconcile with their
    respective bucket of the financial-nature slice of total liabilities,
    Σ ``m50[ntr=m3, rst=m1]`` (witness_lt) and Σ ``m50[ntr=m3, rst=m2]``
    (witness_st) — a *different* rubric, so a genuinely independent witness.
    A filing whose ``m51`` maturity split disagrees with the ``m50`` split — while
    the totals happen to reconcile — would emit a wrong LT/ST split; the whole
    block is suppressed instead. Emit only if both buckets reconcile within tol;
    otherwise suppress and fall back to liabilities-based leverage."""
    keys = ("long_term_debt", "short_term_debt")

    def suppress(reason: str) -> None:
        for key in keys:
            suppressed.append((key, reason))

    # Balance-sheet financial-borrowings tranches (exclude the subordinated
    # ``sts`` cross-cut, which spans — and would double-count — the tranches).
    borrow = [f for f in flat
              if f.get("unit") == "EUR"
              and f["dims"].get("bas") == "m51"
              and f["dims"].get("ntr") == "m3"
              and f["dims"].get("part") == "m3"
              and f["dims"].get("prd") == "m1"
              and "sts" not in f["dims"]]
    if not borrow:
        suppress("no m51 financial-borrowings facts on the balance sheet")
        return

    # Dedup by full dim-tuple before any validation or summing: a malformed
    # filing can duplicate the same context, which would otherwise double-count
    # the borrowings total. Keep the first occurrence (order is document order).
    _seen_b: set[frozenset] = set()
    _deduped: list[dict] = []
    for f in borrow:
        _k = frozenset(f["dims"].items())
        if _k not in _seen_b:
            _seen_b.add(_k)
            _deduped.append(f)
    borrow = _deduped

    # Every remaining fact must be a clean maturity×type tranche. A deviating
    # structure (a breakdown-free subtotal without typ that would double-count, a
    # further sub-breakdown, or an unexpected maturity bucket) means we cannot
    # confirm the total -> suppress.
    for fact in borrow:
        dims = fact["dims"]
        if set(dims) != _TRANCHE_DIMS or dims["rst"] not in ("m1", "m2"):
            suppress("m51 tranche has an unexpected dimension structure "
                     f"(dims={sorted(dims)}) — cannot confirm the borrowings total")
            return

    lt = sum(f["value"] for f in borrow if f["dims"]["rst"] == "m1")
    st = sum(f["value"] for f in borrow if f["dims"]["rst"] == "m2")

    # Independent witness: the financial-nature slice of total liabilities
    # (bas=m50, ntr=m3), tracked per bucket (rst=m1 LT, rst=m2 ST) on the
    # passif (part=m3). Dedup by full dim-tuple to guard against malformed
    # duplicate contexts.
    witness_lt = 0.0
    witness_st = 0.0
    seen_lt = False
    seen_st = False
    _seen_w: set[frozenset] = set()
    for fact in flat:
        dims = fact["dims"]
        if (fact.get("unit") == "EUR"
                and dims.get("bas") == "m50"
                and dims.get("ntr") == "m3"
                and dims.get("part") == "m3"
                and dims.get("rst") in ("m1", "m2")
                and set(dims) == {"bas", "ntr", "part", "prd", "rst"}
                and dims["prd"] == "m1"):
            _wk = frozenset(dims.items())
            if _wk in _seen_w:
                continue
            _seen_w.add(_wk)
            if dims["rst"] == "m1":
                witness_lt += fact["value"]
                seen_lt = True
            else:
                witness_st += fact["value"]
                seen_st = True

    if not (seen_lt or seen_st):
        suppress("no m50[ntr=m3] witness to cross-check the m51 borrowings total")
        return

    # Per-bucket cross-check (NO-FALSE-DATA split guarantee): LT and ST must
    # each reconcile with their respective witness bucket. A filing whose m51
    # split disagrees with the m50[ntr=m3] split — while the totals happen to
    # reconcile — would emit a wrong LT/ST split; suppress the whole block.
    if abs(lt - witness_lt) > _tol(max(abs(lt), abs(witness_lt))):
        suppress(f"m51 LT borrowings {lt} do not reconcile with the independent "
                 f"m50[ntr=m3, rst=m1] witness {witness_lt}")
        return
    if abs(st - witness_st) > _tol(max(abs(st), abs(witness_st))):
        suppress(f"m51 ST borrowings {st} do not reconcile with the independent "
                 f"m50[ntr=m3, rst=m2] witness {witness_st}")
        return

    emit("long_term_debt", lt, "m51 (derived, x-checked)")
    emit("short_term_debt", st, "m51 (derived, x-checked)")


def map_bnb_facts(flat: list[dict], period_end: str | None = None) -> dict:
    """One BNB ``-data.xbrl`` flatten -> curated financials for the current year.

    Only current-year facts (``dims["prd"]=="m1"``) with a EUR unit feed the
    monetary keys. ``period_end`` is passed through (BE's exercise dates are not
    in the fact list — the producer supplies it); ``basis`` is ``"company"``.

    Returns ``{period_end, basis, currency, values, suppressed, unbalanced}``.
    ``values[key] = {"value", "unit":"EUR", "label":key, "tag":<source>}``;
    ``suppressed`` is a list of ``(key, reason)``; ``unbalanced`` is True when
    the primary balance gate fails (then ``values`` is empty)."""
    currency = _currency(flat)
    values: dict[str, dict] = {}
    suppressed: list[tuple[str, str]] = []

    def emit(key: str, value, tag: str) -> None:
        values[key] = {"value": value, "unit": "EUR", "label": key, "tag": tag}

    def result(unbalanced: bool) -> dict:
        return {
            "period_end": period_end, "basis": "company", "currency": currency,
            "values": values, "suppressed": suppressed, "unbalanced": unbalanced,
        }

    # --- Primary gate: total assets (m25/m1) == total passif (m25/m3), both
    #     independently tagged & breakdown-free. Mismatch -> the whole filing is
    #     untrustworthy: unbalanced, emit NO values (wrong > missing). When either
    #     anchor is absent the gate cannot run; we proceed (directly-tagged values
    #     still stand, mirroring the UK sibling) rather than blank everything.
    assets = _select(flat, "m25", "m1", {})
    passif = _select(flat, "m25", "m3", {})
    if assets is not None and passif is not None:
        if abs(assets - passif) > _tol(max(abs(assets), abs(passif))):
            suppressed.append(
                ("__all__",
                 f"unbalanced: total assets {assets} != total passif {passif}"))
            return result(unbalanced=True)

    # --- Canonical concept pack: each key is its breakdown-free total or nothing.
    for key, (bas, part, required) in BE_PACK.items():
        value = _select(flat, bas, part, required)
        if value is None:
            suppressed.append(
                (key, f"no unambiguous breakdown-free fact for bas={bas} part={part}"
                      f" members={required or '{}'}"))
        else:
            emit(key, value, _tag(bas, part, required))

    # --- Financial debt (real borrowings) with the independent cross-check.
    _emit_financial_debt(flat, emit, suppressed)

    # --- Always-suppressed concepts.
    for key, reason in _ALWAYS_SUPPRESS.items():
        suppressed.append((key, reason))

    return result(unbalanced=False)
