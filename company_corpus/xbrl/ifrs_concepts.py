"""IFRS (`ifrs-full`) concept pack — the EU counterpart of the us-gaap CONCEPTS.

Same curated keys as the SEC pack (so the shared engine + derived metrics apply
verbatim and US/EU issuers are comparable), mapped to ``ifrs-full`` tags. ``unit``
is a kind marker only; the real currency is detected from the data per issuer.
"""
from __future__ import annotations

from ..financials import Concept

IFRS_CONCEPTS: tuple[Concept, ...] = (
    # --- Income statement (duration) ---
    Concept("revenue", "Revenue", ("Revenue", "RevenueFromContractsWithCustomers"), False, "EUR"),
    Concept("cost_of_revenue", "Cost of sales", ("CostOfSales",), False, "EUR"),
    Concept("gross_profit", "Gross profit", ("GrossProfit",), False, "EUR"),
    Concept("rnd_expense", "Research & development expense", ("ResearchAndDevelopmentExpense",), False, "EUR"),
    Concept("operating_income", "Operating profit", ("ProfitLossFromOperatingActivities",), False, "EUR"),
    Concept("interest_expense", "Finance costs", ("FinanceCosts",), False, "EUR"),
    Concept("pretax_income", "Profit before tax", ("ProfitLossBeforeTax",), False, "EUR"),
    Concept("income_tax", "Income tax expense", ("IncomeTaxExpenseContinuingOperations",), False, "EUR"),
    Concept("net_income", "Profit (owners of parent)",
            ("ProfitLossAttributableToOwnersOfParent", "ProfitLoss"), False, "EUR"),
    Concept("net_income_nci", "Profit attributable to NCI",
            ("ProfitLossAttributableToNoncontrollingInterests",), False, "EUR"),
    Concept("dep_amort", "Depreciation & amortisation", ("DepreciationAndAmortisationExpense",), False, "EUR"),
    # --- Per share (duration) ---
    Concept("eps_basic", "EPS (basic)", ("BasicEarningsLossPerShare",), False, "EUR/shares"),
    Concept("eps_diluted", "EPS (diluted)", ("DilutedEarningsLossPerShare",), False, "EUR/shares"),
    # --- Cash flow (duration) ---
    Concept("cfo", "Cash from operations", ("CashFlowsFromUsedInOperatingActivities",), False, "EUR"),
    Concept("cfi", "Cash from investing", ("CashFlowsFromUsedInInvestingActivities",), False, "EUR"),
    Concept("cff", "Cash from financing", ("CashFlowsFromUsedInFinancingActivities",), False, "EUR"),
    Concept("capex", "Purchases of PP&E",
            ("PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",), False, "EUR"),
    Concept("dividends_paid", "Dividends paid",
            ("DividendsPaidClassifiedAsFinancingActivities",), False, "EUR"),
    # --- Balance sheet (instant) ---
    Concept("assets", "Total assets", ("Assets",), True, "EUR"),
    Concept("assets_current", "Current assets", ("CurrentAssets",), True, "EUR"),
    Concept("cash", "Cash & equivalents", ("CashAndCashEquivalents",), True, "EUR"),
    Concept("inventory", "Inventories", ("Inventories",), True, "EUR"),
    Concept("receivables", "Trade receivables",
            ("CurrentTradeReceivables", "TradeAndOtherCurrentReceivables"), True, "EUR"),
    Concept("ppe_net", "Property, plant & equipment", ("PropertyPlantAndEquipment",), True, "EUR"),
    Concept("goodwill", "Goodwill", ("Goodwill",), True, "EUR"),
    Concept("intangibles", "Intangible assets (ex-goodwill)", ("IntangibleAssetsOtherThanGoodwill",), True, "EUR"),
    Concept("liabilities", "Total liabilities", ("Liabilities",), True, "EUR"),
    Concept("liabilities_current", "Current liabilities", ("CurrentLiabilities",), True, "EUR"),
    Concept("payables", "Trade payables",
            ("CurrentTradePayables", "TradeAndOtherCurrentPayables"), True, "EUR"),
    Concept("long_term_debt", "Non-current borrowings", ("NoncurrentBorrowings", "Borrowings"), True, "EUR"),
    Concept("short_term_debt", "Current borrowings", ("CurrentBorrowings",), True, "EUR"),
    Concept("equity", "Equity (owners of parent)", ("EquityAttributableToOwnersOfParent", "Equity"), True, "EUR"),
    Concept("equity_total", "Total equity (incl. NCI)", ("Equity",), True, "EUR"),
    Concept("noncontrolling_interest", "Non-controlling interests", ("NoncontrollingInterests",), True, "EUR"),
    Concept("retained_earnings", "Retained earnings", ("RetainedEarnings",), True, "EUR"),
    # --- Shares (instant) ---
    Concept("shares_outstanding", "Shares outstanding", ("NumberOfSharesOutstanding",), True, "shares"),
)

IFRS_CONCEPTS_BY_KEY = {c.key: c for c in IFRS_CONCEPTS}
