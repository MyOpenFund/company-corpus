from __future__ import annotations

import json
import os

import pytest

from company_corpus.config import Config


@pytest.fixture(autouse=True, scope="session")
def _isolated_company_data_dir(tmp_path_factory):
    """Point COMPANY_DATA_DIR at a session-scoped tmp dir for every test.

    Guards against the sister repo's mistake: tests that wrote phantom
    run-reports straight into the checked-out repo's own data/runs.jsonl.
    Session-scoped, so the env var is set and restored by hand rather than
    via pytest's (function-scoped) monkeypatch fixture.
    """
    data_dir = tmp_path_factory.mktemp("data")
    previous = os.environ.get("COMPANY_DATA_DIR")
    os.environ["COMPANY_DATA_DIR"] = str(data_dir)
    try:
        yield data_dir
    finally:
        if previous is None:
            os.environ.pop("COMPANY_DATA_DIR", None)
        else:
            os.environ["COMPANY_DATA_DIR"] = previous


class FakeFetcher:
    """Stand-in for Fetcher that serves canned responses by URL substring.

    ``routes`` maps a substring of the requested URL to either a Python object
    (returned by ``get_json``) or a string (returned by ``get_text``). The first
    matching substring wins; an unmatched URL raises, exercising error paths.
    """

    def __init__(self, routes: dict[str, object], config: Config | None = None):
        self.routes = routes
        self.config = config or Config()
        self.calls: list[str] = []

    def _match(self, url: str):
        self.calls.append(url)
        for needle, value in self.routes.items():
            if needle in url:
                return value
        raise RuntimeError(f"no route for {url}")

    def get_json(self, url: str, **_):
        value = self._match(url)
        if isinstance(value, str):
            return json.loads(value)
        return value

    def get_text(self, url: str, **_):
        value = self._match(url)
        if isinstance(value, str):
            return value
        return json.dumps(value)


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(data_dir=tmp_path / "data", contact="test@example.com")


@pytest.fixture
def make_fetcher(config):
    """Factory: build a FakeFetcher from a routes dict, sharing the test config."""

    def _make(routes: dict[str, object]) -> FakeFetcher:
        return FakeFetcher(routes, config=config)

    return _make


APPLE_SUBMISSIONS = {
    "name": "Apple Inc.",
    "tickers": ["AAPL"],
    "filings": {
        "recent": {
            "form": ["10-K", "10-Q", "8-K", "4", "NT 10-K"],
            "accessionNumber": [
                "0000320193-24-000123",
                "0000320193-24-000081",
                "0000320193-24-000070",
                "0000320193-24-000060",
                "0000320193-24-000050",
            ],
            "filingDate": ["2024-11-01", "2024-08-02", "2024-05-03", "2024-02-01", "2024-01-15"],
            "reportDate": ["2024-09-28", "2024-06-29", "", "", ""],
            "primaryDocument": [
                "aapl-20240928.htm",
                "aapl-20240629.htm",
                "ex991.htm",
                "wk-form4.xml",
                "",
            ],
            "primaryDocDescription": ["10-K", "10-Q", "8-K", "FORM 4", ""],
        },
        "files": [],
    },
}


COMPANY_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 789019, "ticker": "MSFT", "title": "MICROSOFT CORP"},
}


# A minimal but realistic complete submission: header + primary HTML 10-K +
# a text exhibit + a binary graphic (uuencoded payload that must be ignored).
SAMPLE_SUBMISSION = """<SEC-DOCUMENT>0000320193-24-000123.txt : 20241101
<SEC-HEADER>
ACCESSION NUMBER: 0000320193-24-000123
</SEC-HEADER>
<DOCUMENT>
<TYPE>10-K
<SEQUENCE>1
<FILENAME>aapl-20240928.htm
<DESCRIPTION>10-K
<TEXT>
<html><head><style>.x{color:red}</style><title>10-K</title></head>
<body><h1>Annual Report</h1><p>Net sales were   $391 billion.</p>
<script>var x=1;</script></body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-21.1
<SEQUENCE>2
<FILENAME>ex211.htm
<DESCRIPTION>SUBSIDIARIES
<TEXT>
<html><body>Subsidiary list</body></html>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>GRAPHIC
<SEQUENCE>3
<FILENAME>logo.jpg
<TEXT>
begin 644 logo.jpg
M_]C_X``02D9)1@`!`0$`8`!@``#_VP!#``H'!P@'!@H("`@+"@H+#A@0#@T-
end
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""

SAMPLE_SUBMISSION_URL = "0000320193-24-000123.txt"


@pytest.fixture
def sample_submission() -> str:
    return SAMPLE_SUBMISSION


# Minimal company-facts: an annual (FY) and a quarterly (Q3) period, with a 3-month
# vs 9-month revenue duration (to test period-length selection) and a restated
# Assets value (to test latest-filed wins).
SAMPLE_FACTS = {
    "cik": 320193,
    "entityName": "Apple Inc.",
    "facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"label": "Revenue", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 383285000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"},
            {"start": "2023-07-01", "end": "2023-09-30", "val": 89498000000,
             "accn": "acc-q3-23", "fy": 2023, "fp": "Q3", "form": "10-Q", "filed": "2023-08-04"},
            {"start": "2023-01-01", "end": "2023-09-30", "val": 270000000000,
             "accn": "acc-q3-23", "fy": 2023, "fp": "Q3", "form": "10-Q", "filed": "2023-08-04"},
        ]}},
        "NetIncomeLoss": {"label": "Net income", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 96995000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"},
        ]}},
        "Assets": {"label": "Assets", "units": {"USD": [
            {"end": "2023-09-30", "val": 352000000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"},
            {"end": "2023-09-30", "val": 352583000000,
             "accn": "acc-amend", "fy": 2023, "fp": "FY", "form": "10-K/A", "filed": "2024-02-01"},
        ]}},
        # Inputs for the derived block (total debt / EBITDA / FCF / leverage).
        "GrossProfit": {"label": "Gross profit", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 169148000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "OperatingIncomeLoss": {"label": "Operating income", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 114301000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "DepreciationDepletionAndAmortization": {"label": "D&A", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 11519000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "InterestExpense": {"label": "Interest expense", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 3933000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "IncomeTaxExpenseBenefit": {"label": "Income tax", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 16741000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": {
            "label": "Pretax income", "units": {"USD": [
                {"start": "2022-10-01", "end": "2023-09-30", "val": 113736000000,
                 "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "NetCashProvidedByUsedInOperatingActivities": {"label": "CFO", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 110543000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "PaymentsToAcquirePropertyPlantAndEquipment": {"label": "Capex", "units": {"USD": [
            {"start": "2022-10-01", "end": "2023-09-30", "val": 10959000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "LongTermDebtNoncurrent": {"label": "LT debt", "units": {"USD": [
            {"end": "2023-09-30", "val": 95281000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "LongTermDebtCurrent": {"label": "LT debt current", "units": {"USD": [
            {"end": "2023-09-30", "val": 9822000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "CommercialPaper": {"label": "Commercial paper", "units": {"USD": [
            {"end": "2023-09-30", "val": 5985000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "StockholdersEquity": {"label": "Equity", "units": {"USD": [
            {"end": "2023-09-30", "val": 62146000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "AssetsCurrent": {"label": "Current assets", "units": {"USD": [
            {"end": "2023-09-30", "val": 143566000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "LiabilitiesCurrent": {"label": "Current liabilities", "units": {"USD": [
            {"end": "2023-09-30", "val": 145308000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "InventoryNet": {"label": "Inventory", "units": {"USD": [
            {"end": "2023-09-30", "val": 6331000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "CashAndCashEquivalentsAtCarryingValue": {"label": "Cash", "units": {"USD": [
            {"end": "2023-09-30", "val": 29965000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
        "ShortTermInvestments": {"label": "ST investments", "units": {"USD": [
            {"end": "2023-09-30", "val": 31590000000,
             "accn": "acc-fy23", "fy": 2023, "fp": "FY", "form": "10-K", "filed": "2023-11-01"}]}},
    }},
}

SAMPLE_FACTS_SUBMISSIONS = {"entityName": "Apple Inc.", "formerNames": [], "sic": "3571"}


@pytest.fixture
def sample_facts() -> dict:
    return SAMPLE_FACTS


@pytest.fixture
def xbrl_fetcher(config) -> FakeFetcher:
    return FakeFetcher(
        {
            "api/xbrl/companyfacts/CIK0000320193.json": SAMPLE_FACTS,
            "submissions/CIK0000320193.json": SAMPLE_FACTS_SUBMISSIONS,
        },
        config=config,
    )


@pytest.fixture
def apple_fetcher(config) -> FakeFetcher:
    return FakeFetcher(
        {
            "CIK0000320193.json": APPLE_SUBMISSIONS,
            "company_tickers.json": COMPANY_TICKERS,
            SAMPLE_SUBMISSION_URL: SAMPLE_SUBMISSION,
        },
        config=config,
    )
