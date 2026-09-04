from __future__ import annotations

from company_corpus.indices import sp500_changes, sp500_current, sp500_membership
from company_corpus.universe import issuers_from_sp500

# Canned Wikipedia-style page: a current-constituents table (with CIK) + a
# multi-header changes table.
WIKI_HTML = """
<table class="wikitable">
 <thead><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>CIK</th></tr></thead>
 <tbody>
  <tr><td>AAPL</td><td>Apple Inc.</td><td>IT</td><td>320193</td></tr>
  <tr><td>MSFT</td><td>Microsoft Corp</td><td>IT</td><td>789019</td></tr>
  <tr><td>BRK.B</td><td>Berkshire Hathaway</td><td>Financials</td><td>1067983</td></tr>
 </tbody>
</table>
<table class="wikitable">
 <thead>
  <tr><th>Date</th><th colspan="2">Added</th><th colspan="2">Removed</th><th>Reason</th></tr>
  <tr><th>Date</th><th>Ticker</th><th>Security</th><th>Ticker</th><th>Security</th><th>Reason</th></tr>
 </thead>
 <tbody>
  <tr><td>June 20, 2023</td><td>AAPL</td><td>Apple Inc.</td><td>OLDCO</td><td>Old Company</td><td>M&amp;A</td></tr>
  <tr><td>March 15, 2012</td><td>MSFT</td><td>Microsoft Corp</td><td>ANCIENT</td><td>Ancient Co</td><td>Delisted</td></tr>
 </tbody>
</table>
"""

COMPANY_TICKERS = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    # OLDCO still maps (simulating an active symbol); ANCIENT is absent (delisted).
    "1": {"cik_str": 999001, "ticker": "OLDCO", "title": "Old Company"},
}


def _fetcher(make_fetcher):
    return make_fetcher({"List_of_S": WIKI_HTML, "company_tickers.json": COMPANY_TICKERS})


def test_sp500_current(make_fetcher):
    cur = sp500_current(_fetcher(make_fetcher))
    by = {c["ticker"]: c for c in cur}
    assert by["AAPL"]["cik"] == "0000320193"
    assert by["BRK-B"]["cik"] == "0001067983"  # BRK.B normalized
    assert by["MSFT"]["company"] == "Microsoft Corp"


def test_sp500_changes(make_fetcher):
    ch = sp500_changes(_fetcher(make_fetcher))
    assert {c["date"] for c in ch} == {"2023-06-20", "2012-03-15"}
    add23 = next(c for c in ch if c["date"] == "2023-06-20")
    assert add23["added"] == "AAPL" and add23["removed"] == "OLDCO"


def test_membership_union_and_dates(make_fetcher):
    members, changes = sp500_membership(_fetcher(make_fetcher))
    by = {m["ticker"]: m for m in members}
    assert set(by) == {"AAPL", "MSFT", "BRK-B", "OLDCO", "ANCIENT"}
    assert by["AAPL"]["last_seen"] == "current" and by["AAPL"]["first_seen"] == "2023-06-20"
    assert by["OLDCO"]["last_seen"] == "2023-06-20" and by["OLDCO"]["cik"] == ""
    assert by["BRK-B"]["first_seen"] == ""  # no add event in the changes table
    assert len(changes) == 2


def test_membership_window_excludes_old(make_fetcher):
    members, _ = sp500_membership(_fetcher(make_fetcher), start="2015")
    tickers = {m["ticker"] for m in members}
    assert "ANCIENT" not in tickers   # removed 2012, before window
    assert "OLDCO" in tickers          # removed 2023, in window


def test_issuers_from_sp500_current_only(make_fetcher):
    issuers, changes, unresolved = issuers_from_sp500(_fetcher(make_fetcher), current_only=True)
    assert len(issuers) == 3 and all(i.cik for i in issuers)
    assert changes == [] and unresolved == []
    assert all(i.last_seen == "current" for i in issuers)


def test_issuers_from_sp500_historical_resolves_and_flags(make_fetcher):
    issuers, changes, unresolved = issuers_from_sp500(_fetcher(make_fetcher))
    by = {i.ticker: i for i in issuers}
    assert by["OLDCO"].cik == "0000999001"   # resolved via SEC map
    assert by["ANCIENT"].cik == ""           # not in map -> unresolved
    assert unresolved == ["ANCIENT"]
    assert len(changes) == 2
