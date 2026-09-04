"""Map Luxembourg LBR/STATEC eCDF declarations to our curated concepts.

Consumes one declarer's declaration list from
:func:`company_corpus.registers.lu_ecdf.parse_lu_declarers` — each item
``{"type","model","currency","period_end","fields": {ecdf_int: value}}`` — and
produces the same shape as the BE/UK/CH siblings
(:func:`company_corpus.registers.concepts_be.map_bnb_facts`,
:func:`company_corpus.registers.concepts_uk.map_ch_facts`):
``{period_end, basis, currency, values, suppressed, unbalanced}`` where
``values[key] = {"value", "unit":"EUR", "label", "tag":"ecdf:NNN"}``.

GOVERNING PRINCIPLE — NO FALSE DATA. This corpus is open data for a credit
universe: a number we cannot confirm must never be emitted; a *missing* number
is strictly better than a *wrong* one. So every ambiguous or unconfirmed value
is suppressed with a recorded reason.

Luxembourg's eCDF has three real traps, handled exactly here:

1. **Two taxonomy versions.** 2016+ ("2022") vs 2012 differ on some codes.
   Detect via a version-EXCLUSIVE 2016+ code (any of ``669``/``435``/``437``)
   present -> 2022, else 2012. Keying only off ``669`` (a P&L code) would misread
   a 2022 balance sheet filed without a P&L declaration as 2012. The financial-
   debt breakdown, the total-liabilities code and the net-income code differ by
   version; the BS core (201/301/331/197/…) is stable.

2. **A signed P&L (2022 only).** The 2022 P&L stores expenses negative, so
   ``interest_expense = abs(627)`` and ``income_tax = -635`` (raw 635 negative
   for an expense, positive for a benefit). The 2012 P&L is unsigned positive.

3. **The 667-vs-669 net-income confusion.** ``667`` is the result *after
   income tax but before other taxes*; ``669 = 667 + 637`` is the FINAL result.
   Net income is ``669`` (2022) or ``639 − 735`` (2012) — **never 667**. If the
   2022 declaration carries 667 but not 669 we suppress rather than fall back.

BS codes are < 600 and P&L codes are >= 600, so merging an entity's balance-
sheet and profit-and-loss declarations into one ``{ecdf: value}`` map is
collision-free. An unreported line is a genuine zero on the LU balance sheet,
so absent components read as 0 in the structural identities.

The confidence gate (design §4), tolerance ``max(2, 0.005·scale)``:
  (a) **Primary** ``201 == 405`` (total assets == total passif) -> else the
      filing is untrustworthy: ``unbalanced=True``, no values.
  (b) **Structural** ``301 + 331 + (435|339) + 403 == 405`` -> else every
      passif-derived value (equity, provisions, liabilities, net_result_bs, and
      the whole debt block) is suppressed.
  (c) **BS/P&L** ``321 == net_income`` -> else suppress net_income (guards
      against merging mismatched declarations).
The engine (:func:`company_corpus.financials.compute_derived`) builds
``total_debt`` from ``long_term_debt`` + ``short_term_debt`` and has NO
``financial_debt`` key, so we emit that maturity split — but only when it
reconciles with the bonds + bank borrowings total (``ST + LT == bonds+bank``,
an independent cross-check). Otherwise, and for abridged / SOPARFI declarations
(which report only aggregate liabilities, not borrowings), the whole debt block
is suppressed (fall back to liabilities-based leverage rather than a wrong
number).
"""
from __future__ import annotations

from ._common import _tol

# Balance-sheet / P&L declaration type names.
_FULL_BILAN = "CA_BILAN"


def map_lu_entity(declarations: list[dict]) -> dict:
    """Merge one entity's BS + P&L declarations into curated financials.

    ``declarations`` is the ``declarations`` list of a single
    :func:`parse_lu_declarers` declarer (only current-year ``<Data>`` — T1
    already returns that). Returns
    ``{period_end, basis, currency, values, suppressed, unbalanced}`` with
    ``basis="company"``, currency EUR. ``suppressed`` is a list of
    ``(key, reason)`` for every value declined; ``unbalanced`` is True when the
    primary balance gate fails (then ``values`` is empty)."""
    # Period-consistency guard: a caller passing two periods of one RCS must not
    # silently blend them. Keep only the declarations at the LATEST period_end so
    # the merge uses one consistent period (period-less declarations and older
    # periods are dropped). No period_end anywhere -> nothing to filter.
    _periods = [d["period_end"] for d in declarations if d.get("period_end")]
    if _periods:
        _max_period = max(_periods)
        declarations = [d for d in declarations if d.get("period_end") == _max_period]

    # Merge all declarations' fields (BS < 600, P&L >= 600 — no collision).
    fields: dict[int, float] = {}
    for d in declarations:
        fields.update(d.get("fields", {}))
    types = {d.get("type", "") for d in declarations}

    # Taxonomy version: a version-EXCLUSIVE 2016+ code (669/435/437) present ->
    # 2016+ ("2022"), else 2012. Never key off 669 alone (a P&L code): a 2022 BS
    # filed without a P&L would misread as 2012 and read the wrong debt codes.
    v2022 = any(c in fields for c in (669, 435, 437))

    # period_end / currency: first declaration that supplies each; EUR default.
    period_end = next((d["period_end"] for d in declarations if d.get("period_end")), None)
    currency = next((d["currency"] for d in declarations if d.get("currency")), "EUR")

    values: dict[str, dict] = {}
    suppressed: list[tuple[str, str]] = []

    def has(code: int) -> bool:
        return code in fields

    def get(code: int) -> float:
        return fields.get(code, 0.0)

    def emit(key: str, value, tag: str) -> None:
        values[key] = {"value": value, "unit": "EUR", "label": key, "tag": tag}

    def result(unbalanced: bool) -> dict:
        return {
            "period_end": period_end, "basis": "company", "currency": currency,
            "values": values, "suppressed": suppressed, "unbalanced": unbalanced,
        }

    # --- Gate (a) Primary: total assets (201) == total passif (405). Mismatch
    #     -> the whole filing is untrustworthy: unbalanced, emit NO values. When
    #     an anchor is absent the gate cannot run (mirrors the BE/UK siblings);
    #     directly-tagged values below still stand.
    if has(201) and has(405):
        a, p = fields[201], fields[405]
        if abs(a - p) > _tol(max(abs(a), abs(p))):
            suppressed.append(
                ("__all__", f"unbalanced: total assets {a} != total passif {p}"))
            return result(unbalanced=True)

    # Total-liabilities (creditors) code: 435 (2022) / 339 (2012) — version-driven
    # ONLY. 2022 filers (SOPARFI included) report the aggregate under 435; forcing
    # 339 for a 2022 SOPARFI would fail the structural gate and suppress the entire
    # holdco universe. Let the structural gate (b) below decide reconciliation.
    liab_code = 435 if v2022 else 339

    # --- Gate (b) Structural: 301 + 331 + (435|339) + 403 == 405. Absent lines
    #     read as 0 (genuine zeros). On failure the passif decomposition is
    #     untrustworthy -> every passif-derived value is suppressed below.
    structural_ok = True
    if has(405):
        struct_sum = get(301) + get(331) + get(liab_code) + get(403)
        if abs(struct_sum - fields[405]) > _tol(max(abs(struct_sum), abs(fields[405]))):
            structural_ok = False

    # --- Actif-side + P&L values (independent of the passif structural gate).
    if has(201):
        emit("assets", get(201), "ecdf:201")
    else:
        suppressed.append(("assets", "no ecdf:201 (total assets)"))
    if has(197):
        emit("cash", get(197), "ecdf:197")
    else:
        suppressed.append(("cash", "no ecdf:197"))

    # revenue / participation income — always positive; abridged & SOPARFI omit them.
    if has(701):
        emit("revenue", get(701), "ecdf:701")
    else:
        suppressed.append(("revenue", "no ecdf:701 (abridged/SOPARFI omit revenue)"))
    if has(715):
        emit("participation_income", get(715), "ecdf:715")
    else:
        suppressed.append(("participation_income", "no ecdf:715"))

    # income_tax: 2022 P&L is signed (635 negative=expense, positive=benefit) so
    # emit -635 (positive = expense); 2012 is unsigned -> positive absolute.
    if has(635):
        income_tax = -get(635) if v2022 else abs(get(635))
        emit("income_tax", income_tax, "ecdf:635")
    else:
        suppressed.append(("income_tax", "no ecdf:635"))
    # interest_expense: abs(627) in both versions (2022 stores it negative).
    if has(627):
        emit("interest_expense", abs(get(627)), "ecdf:627")
    else:
        suppressed.append(("interest_expense", "no ecdf:627"))

    # --- Passif-derived values, gated by the structural identity (b).
    passif = (
        ("equity", 301, "ecdf:301"),
        ("provisions", 331, "ecdf:331"),
        ("net_result_bs", 321, "ecdf:321"),
        ("liabilities", liab_code, f"ecdf:{liab_code}"),
    )
    for key, code, tag in passif:
        if not structural_ok:
            suppressed.append(
                (key, f"structural gate failed: 301+331+{liab_code}+403 != 405"))
        elif has(code):
            emit(key, get(code), tag)
        else:
            suppressed.append((key, f"no ecdf:{code}"))

    # --- net_income (P&L) + gate (c) cross-check against the BS result (321).
    #     NEVER 667: 669 (2022) is the final result; 667 is pre-other-taxes.
    net_income = None
    ni_tag = ""
    if v2022:
        if has(669):
            net_income, ni_tag = get(669), "ecdf:669"
        else:
            reason = ("667 present but 669 absent — refusing to fall back to 667"
                      if has(667) else "no ecdf:669 (final net result)")
            suppressed.append(("net_income", reason))
    else:  # 2012: 639 profit − 735 loss (either may be absent -> 0).
        if has(639) or has(735):
            net_income, ni_tag = get(639) - get(735), "ecdf:639-735"
        else:
            suppressed.append(("net_income", "no ecdf:639/735 (2012 net result)"))

    if net_income is not None:
        # Gate (c): 321 (BS result) must reconcile with the P&L net income, else
        # the merged BS/P&L are likely mismatched declarations -> suppress.
        if has(321) and abs(get(321) - net_income) > _tol(max(abs(get(321)), abs(net_income))):
            suppressed.append(
                ("net_income",
                 f"BS/P&L mismatch: net_result_bs {get(321)} != net_income {net_income}"))
        else:
            emit("net_income", net_income, ni_tag)

    # --- Debt split (long_term_debt/short_term_debt — the engine-consumed
    #     borrowings). Full BS only; the structural gate must hold (passif
    #     breakdown); emitted only when the maturity split reconciles.
    _emit_financial_debt(v2022, types, has, get, emit, suppressed, structural_ok)

    return result(unbalanced=False)


def _emit_financial_debt(v2022, types, has, get, emit, suppressed, structural_ok) -> None:
    """Emit the ``long_term_debt``/``short_term_debt`` maturity split (real
    borrowings — the payoff), or suppress the whole block with a recorded reason.

    The engine (:func:`company_corpus.financials.compute_derived`) builds
    ``total_debt`` from ``long_term_debt`` + ``short_term_debt`` and never reads a
    ``financial_debt`` key, so we emit the split the engine consumes — matching the
    BE sibling (:func:`concepts_be._emit_financial_debt`). No dead ``financial_debt``.

    Suppressed wholesale for abridged / SOPARFI declarations (they report only
    aggregate liabilities, incl. trade payables, not the borrowings isolation) and
    when the structural passif gate failed. NO-FALSE-DATA reconciliation: the bond
    + bank borrowings total and the ST/LT maturity sums are kept internal; the
    split is emitted ONLY when it reconciles (``|ST + LT − (bonds+bank)| <= tol``,
    an independent cross-check). If it does not reconcile the split is
    unconfirmable -> suppress the block (fall back to liabilities-based leverage
    rather than emit a wrong number)."""
    keys = ("long_term_debt", "short_term_debt")

    def suppress_all(reason: str) -> None:
        for key in keys:
            suppressed.append((key, reason))

    # A full CA_BILAN is required — abridged/SOPARFI give only the aggregate.
    if _FULL_BILAN not in types:
        suppress_all(
            "no full CA_BILAN — abridged/SOPARFI report aggregate liabilities, "
            "not borrowings")
        return
    if not structural_ok:
        suppress_all("structural gate failed — passif breakdown untrustworthy")
        return

    if v2022:
        bond_c, bank_c = 437, 355
        st_codes, lt_codes = (441, 447, 357), (443, 449, 359)
    else:
        bond_c, bank_c = 341, 355
        st_codes, lt_codes = (351, 357), (347, 353, 359)

    if not (has(bond_c) or has(bank_c)):
        suppress_all(f"no ecdf:{bond_c}/{bank_c} — cannot form the borrowings total")
        return

    borrowings = get(bond_c) + get(bank_c)  # internal cross-check target only
    st = sum(get(c) for c in st_codes)
    lt = sum(get(c) for c in lt_codes)
    if abs((st + lt) - borrowings) <= _tol(max(abs(st + lt), abs(borrowings))):
        emit("long_term_debt", lt, "ecdf:" + "+".join(str(c) for c in lt_codes))
        emit("short_term_debt", st, "ecdf:" + "+".join(str(c) for c in st_codes))
    else:
        suppress_all(
            f"maturity split ST+LT {st + lt} != borrowings (bonds+bank) "
            f"{borrowings} — split unconfirmable, suppressing the debt block")
