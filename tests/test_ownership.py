from __future__ import annotations

from datetime import date

from company_corpus.models import FilingRecord
from company_corpus.ownership import (
    find_ownership_doc,
    form345_rows,
    form345_text,
    parse_13f,
    parse_form345,
    render_13f_html,
    render_form345_html,
    thirteenf_rows,
)
from company_corpus.pipeline import process_ownership
from company_corpus.storage import Storage
from company_corpus.taxonomy import FormType

FORM4_SUBMISSION = """<SEC-DOCUMENT>acc-f4.txt : 20240501
<SEC-HEADER></SEC-HEADER>
<DOCUMENT>
<TYPE>4
<SEQUENCE>1
<FILENAME>form4.xml
<TEXT>
<XML>
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2024-04-30</periodOfReport>
  <issuer><issuerCik>0000320193</issuerCik><issuerName>Apple Inc.</issuerName><issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerCik>0001214156</rptOwnerCik><rptOwnerName>COOK TIMOTHY D</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isOfficer>true</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2024-04-30</value></transactionDate>
      <transactionCoding><transactionCode>S</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>511000</value></transactionShares>
        <transactionPricePerShare><value>170.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts><sharesOwnedFollowingTransaction><value>3280000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""

THIRTEENF_SUBMISSION = """<SEC-DOCUMENT>acc-13f.txt : 20240501
<SEC-HEADER></SEC-HEADER>
<DOCUMENT>
<TYPE>13F-HR
<SEQUENCE>1
<FILENAME>primary_doc.xml
<TEXT>
<edgarSubmission><headerData/></edgarSubmission>
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>INFORMATION TABLE
<SEQUENCE>2
<FILENAME>infotable.xml
<TEXT>
<XML>
<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable><nameOfIssuer>APPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>037833100</cusip><value>1000000</value><shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
  <infoTable><nameOfIssuer>MICROSOFT CORP</nameOfIssuer><titleOfClass>COM</titleOfClass><cusip>594918104</cusip><value>2500000</value><shrsOrPrnAmt><sshPrnamt>6000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt></infoTable>
</informationTable>
</XML>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""

SC13D_SUBMISSION = """<SEC-DOCUMENT>acc-13d.txt : 20240501
<SEC-HEADER></SEC-HEADER>
<DOCUMENT>
<TYPE>SC 13D
<SEQUENCE>1
<FILENAME>sc13d.htm
<TEXT>
<html><body><p>The reporting person beneficially owns 6.5% of the common stock.</p></body></html>
</TEXT>
</DOCUMENT>
</SEC-DOCUMENT>
"""


# ---- Form 3/4/5 ----
def test_parse_form4_owner_role_and_transaction():
    xml = find_ownership_doc(FORM4_SUBMISSION, "E1")
    assert xml is not None
    f = parse_form345(xml)
    assert f.owner_name == "COOK TIMOTHY D"
    assert f.is_officer and f.officer_title == "CEO"
    assert "Officer (CEO)" in f.role
    assert f.issuer_symbol == "AAPL"
    assert len(f.transactions) == 1
    t = f.transactions[0]
    assert t.code == "S" and t.acquired_disposed == "D"
    assert t.shares == "511000" and t.price == "170.5" and t.shares_after == "3280000"


def test_form4_render_and_rows():
    f = parse_form345(find_ownership_doc(FORM4_SUBMISSION, "E1"))
    html = render_form345_html(f)
    assert "COOK TIMOTHY D" in html and "Common Stock" in html and "170.5" in html
    assert "disposed" in form345_text(f)
    rows = form345_rows("0000320193", "acc-f4", f)
    assert len(rows) == 1 and rows[0]["code"] == "S" and rows[0]["doc_type"] == "E1"


# ---- 13F ----
def test_parse_13f_holdings_and_aggregates():
    xml = find_ownership_doc(THIRTEENF_SUBMISSION, "E2")
    assert xml is not None
    holdings, agg = parse_13f(xml)
    assert len(holdings) == 2
    assert agg["num_positions"] == 2
    assert agg["total_value"] == 3500000
    assert agg["top"][0].issuer == "MICROSOFT CORP"  # largest by value
    rows = thirteenf_rows("0001067983", "acc-13f", holdings, date(2024, 5, 1))
    assert len(rows) == 2 and rows[0]["doc_type"] == "E2"


def test_render_13f_html():
    holdings, agg = parse_13f(find_ownership_doc(THIRTEENF_SUBMISSION, "E2"))
    html = render_13f_html(holdings, agg, filer="Berkshire Hathaway", report="2024-03-31")
    assert "Berkshire Hathaway" in html and "MICROSOFT CORP" in html and "2,500,000" in html


def test_find_ownership_doc_missing():
    assert find_ownership_doc("no documents", "E1") is None


# ---- pipeline ----
def _seed(storage, **kw):
    rec = FilingRecord(filing_date=date(2024, 5, 1), **kw)
    storage.save_records([rec], dry_run=False)
    return rec


def test_process_ownership_parses_insider_and_13f(make_fetcher, config):
    st = Storage(config)
    _seed(st, cik="320193", form_type=FormType.E1, sec_form="4", accession="acc-f4",
          company="Apple Inc.", primary_doc_url="https://x/form4.xml",
          submission_url="https://sec/form4sub.txt")
    _seed(st, cik="320193", form_type=FormType.E2, sec_form="13F-HR", accession="acc-13f",
          company="Berkshire", submission_url="https://sec/13fsub.txt")

    fetcher = make_fetcher({"form4sub.txt": FORM4_SUBMISSION, "13fsub.txt": THIRTEENF_SUBMISSION})
    rep = process_ownership(["320193"], dry_run=False, config=config, fetcher=fetcher, storage=st)

    assert rep.parsed_insider == 1 and rep.parsed_13f == 1
    manifest = st.load_manifest("320193")
    e1 = next(r for r in manifest.values() if r.form_type is FormType.E1)
    assert e1.text_path and "COOK TIMOTHY D" in (config.data_dir / e1.text_path).read_text()
    # Normalized ownership rows written (1 insider txn + 2 holdings).
    rows = (config.ownership_dir / "0000320193.jsonl").read_text().splitlines()
    assert len(rows) == 3


def test_process_ownership_e3_passthrough(make_fetcher, config):
    st = Storage(config)
    _seed(st, cik="320193", form_type=FormType.E3, sec_form="SC 13D", accession="acc-13d",
          company="Apple Inc.", primary_doc_url="https://x/sc13d.htm",
          submission_url="https://sec/13dsub.txt")
    fetcher = make_fetcher({"13dsub.txt": SC13D_SUBMISSION})
    rep = process_ownership(["320193"], dry_run=False, config=config, fetcher=fetcher, storage=st)
    assert rep.passthrough == 1 and rep.parsed_insider == 0
    e3 = next(r for r in st.load_manifest("320193").values())
    # Narrative text kept from the generic extraction path.
    assert e3.text_path and "beneficially owns 6.5%" in (config.data_dir / e3.text_path).read_text()


def test_process_ownership_dry_run_writes_nothing(make_fetcher, config):
    st = Storage(config)
    _seed(st, cik="320193", form_type=FormType.E1, sec_form="4", accession="acc-f4",
          submission_url="https://sec/form4sub.txt")
    fetcher = make_fetcher({"form4sub.txt": FORM4_SUBMISSION})
    rep = process_ownership(["320193"], dry_run=True, config=config, fetcher=fetcher, storage=st)
    assert rep.parsed_insider == 0
    assert not (config.data_dir / "raw").exists()
    assert not config.ownership_dir.exists()


def test_thirteenf_rows_value_unit_boundary():
    from company_corpus.ownership import Holding, thirteenf_rows

    h = Holding(issuer="ACME CORP", title_of_class="COM", cusip="000000000",
                value=1000, shares=10, share_type="SH")

    pre_rows = thirteenf_rows("0001234567", "acc-pre", [h], date(2022, 12, 31))
    assert len(pre_rows) == 1
    assert pre_rows[0]["value_unit"] == "USD_thousands"
    assert pre_rows[0]["value"] == 1000

    boundary_rows = thirteenf_rows("0001234567", "acc-bnd", [h], date(2023, 1, 3))
    assert len(boundary_rows) == 1
    assert boundary_rows[0]["value_unit"] == "USD"
    assert boundary_rows[0]["value"] == 1000

    post_rows = thirteenf_rows("0001234567", "acc-post", [h], date(2024, 1, 1))
    assert len(post_rows) == 1
    assert post_rows[0]["value_unit"] == "USD"
    assert post_rows[0]["value"] == 1000
