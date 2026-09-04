"""Showcase the UK confidence gate — NO FALSE DATA governing principle (offline).

UK iXBRL filings carry no universally-tagged TotalAssets or TotalDebt: every
total must be derived from structural anchors, and each derivation is gated.
A number we know is wrong must NEVER be emitted; a missing value is strictly
better than a wrong one. Four real-world cases:

  1. Full clean filer    — all items emitted (assets=6,270,386; liabilities=216,826)
  2. FixedAssets untagged — assets via TALCL = 9,435,020 (NOT the 1,107,327 trap)
  3. P&L-only filer      — revenue / net_income / equity emitted; balance suppressed
  4. Unbalanced filing   — NetAssets != Equity -> entire filing rejected

    ./venv/bin/python examples/28_uk_confidence_gate.py
"""
from __future__ import annotations

from company_corpus.registers.concepts_uk import map_ch_facts


def flat(**kw: float) -> dict:
    """Synthetic OIM flatten: one tagged fact per concept at period-end 2025-03-31."""
    return {
        name: [{"val": v, "end": "2025-03-31", "unit": "GBP", "tag": name, "label": name}]
        for name, v in kw.items()
    }


_SHOW_KEYS = (
    "revenue", "net_income",
    "equity", "cash",
    "assets", "liabilities", "liabilities_current", "long_term_debt",
)


def _show(label: str, m: dict) -> None:
    print(f"\n--- {label} ---")
    if m is None:
        print("  (no current period in facts)")
        return
    if m["unbalanced"]:
        reason = m["suppressed"][0][1] if m.get("suppressed") else "?"
        print(f"  UNBALANCED — entire filing rejected, zero values emitted")
        print(f"  reason     : {reason}")
        return
    v = m["values"]
    for key in _SHOW_KEYS:
        if key in v:
            print(f"  EMIT   {key:30} {v[key]['value']:>14,.0f}  {v[key]['unit']}")
    # Group suppressed keys by their shared reason string (the derived block shares one)
    by_reason: dict[str, list[str]] = {}
    for key, reason in m.get("suppressed", []):
        if key == "__all__":
            continue
        by_reason.setdefault(reason, []).append(key)
    for reason, keys in by_reason.items():
        print(f"  SUPPR  {', '.join(keys)}")
        # Truncate long reasons for readability; full text is in the coverage report
        print(f"         reason: {reason[:90]}")


# ---------------------------------------------------------------------------
print("GOVERNING PRINCIPLE: missing > wrong — we never emit a number we cannot confirm.")
print("map_ch_facts gates every derivation on structural anchors independently tagged.")
# ---------------------------------------------------------------------------

# Case 1 — full clean filer: all balance-sheet + P&L items emitted
# FixedAssets tagged -> anchor check passes. NetAssets == Equity passes.
# assets  = TALCL(6129560) + liabilities_current(2625095−2484269=140826) = 6,270,386
# liabs   = liabilities_current(140826) + long_term_debt(6129560−6053560=76000) = 216,826
m1 = map_ch_facts(flat(
    FixedAssets=3645291, CurrentAssets=2625095,
    NetCurrentAssetsLiabilities=2484269,
    TotalAssetsLessCurrentLiabilities=6129560,
    NetAssetsLiabilities=6053560, Equity=6053560,
    ProfitLoss=161709, CashBankOnHand=2095623,
))
assert not m1["unbalanced"]
assert m1["values"]["assets"]["value"] == 6_270_386
assert m1["values"]["liabilities"]["value"] == 216_826
_show("Case 1: full clean filer (all items emitted — assets=6,270,386  liabilities=216,826)", m1)

# Case 2 — FixedAssets untagged: assets via TALCL, NOT FixedAssets+CurrentAssets
# If we had used FixedAssets+CurrentAssets we would only see 1,107,327 (wrong, understated).
# Instead: assets = TALCL(8893190) + liabilities_current(1107327−565497=541830) = 9,435,020
m2 = map_ch_facts(flat(
    CurrentAssets=1107327, NetCurrentAssetsLiabilities=565497,
    TotalAssetsLessCurrentLiabilities=8893190,
    NetAssetsLiabilities=7521425, Equity=7521425,
    CashBankOnHand=47547,
))
assert m2["values"]["assets"]["value"] == 9_435_020   # NOT 1,107,327
_show(
    "Case 2: FixedAssets untagged — assets=9,435,020 via TALCL  (NOT 1,107,327 understatement trap)",
    m2,
)

# Case 3 — P&L-only filer: TALCL and NCA absent -> liability/debt block suppressed
# Without both TALCL and NetCurrentAssets we cannot derive either half of the debt
# block — emitting one half would silently understate total_debt. So assets and
# liabilities are suppressed while directly-tagged revenue/equity/cash still stand.
m3 = map_ch_facts(flat(
    TurnoverRevenue=30927, ProfitLoss=15894,
    CurrentAssets=22922, NetAssetsLiabilities=18260, Equity=18260,
    CashBankOnHand=10759,
))
assert "assets" not in m3["values"]
assert "liabilities" not in m3["values"]
assert m3["values"]["revenue"]["value"] == 30927
_show(
    "Case 3: P&L-only filer (no TALCL / NCA) — revenue/equity emitted, balance suppressed",
    m3,
)

# Case 4 — unbalanced: |NetAssets − Equity| > tol(max(|NA|,|E|)) -> whole filing rejected
# tol(1200) = max(2, 0.005*1200) = 6.0; |1000 − 1200| = 200 >> 6 -> unbalanced.
m4 = map_ch_facts(flat(
    NetAssetsLiabilities=1000, Equity=1200,
    CurrentAssets=5000, NetCurrentAssetsLiabilities=1200,
    TotalAssetsLessCurrentLiabilities=1000,
))
assert m4["unbalanced"] is True
assert m4["values"] == {}
_show(
    "Case 4: unbalanced (NetAssets=1,000  !=  Equity=1,200  |diff|=200 > tol=6)",
    m4,
)
