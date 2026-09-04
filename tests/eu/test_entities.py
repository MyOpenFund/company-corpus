import json
from pathlib import Path
from company_corpus.eu.entities import resolve_entities

FIX = Path(__file__).parent.parent / "fixtures" / "eu"


class _Fetcher:
    def __init__(self, routes): self.routes = routes; self.calls = []
    def get_json(self, url, **_):
        self.calls.append(url)
        for needle, val in self.routes.items():
            if needle in url:
                return json.loads(val) if isinstance(val, str) else val
        raise RuntimeError(f"no route for {url}")


def test_resolve_by_lei_uses_gleif_record():
    f = _Fetcher({"lei-records/529900D6BF99LW9R2E68": (FIX / "gleif_lei_sap.json").read_text()})
    [e] = resolve_entities([{"lei": "529900D6BF99LW9R2E68"}], fetcher=f)
    assert e.lei == "529900D6BF99LW9R2E68"
    assert e.name == "SAP SE"
    assert e.country == "DE"
    assert e.resolution == "lei"


def test_resolve_by_name_ambiguous_country_is_unresolved():
    # The re-captured gleif_name_sap.json (page[size]=10) returns 3 records total;
    # filtering to country="DE" leaves 2 candidates (SAP SE + SAP Deutschland SE & Co. KG).
    # Under the new rule (exactly-one wins), this must be unresolved — never bind to wrong LEI.
    f = _Fetcher({"filter": (FIX / "gleif_name_sap.json").read_text()})
    [e] = resolve_entities([{"name": "SAP SE", "country": "DE"}], fetcher=f)
    assert e.lei is None
    assert e.resolution == "unresolved"


def test_ambiguous_name_resolves_unresolved():
    # Two records both with country="FR" → ambiguous → unresolved.
    f = _Fetcher({"filter": (FIX / "gleif_name_ambiguous.json").read_text()})
    [e] = resolve_entities([{"name": "Foo", "country": "FR"}], fetcher=f)
    assert e.lei is None
    assert e.resolution == "unresolved"


def test_unique_name_resolves_with_name_tier():
    # Single record with country="FR" → exactly one candidate → resolves.
    f = _Fetcher({"filter": (FIX / "gleif_name_unique.json").read_text()})
    [e] = resolve_entities([{"name": "UniqueBar SA", "country": "FR"}], fetcher=f)
    assert e.lei == "FR0000000000000000C3"
    assert e.resolution == "name"


def test_unresolvable_is_recorded_not_guessed():
    f = _Fetcher({"filter": json.dumps({"data": [], "meta": {"pagination": {"total": 0}}})})
    [e] = resolve_entities([{"name": "Nonexistent Co", "country": "FR"}], fetcher=f)
    assert e.lei is None and e.resolution == "unresolved"


def test_resolve_by_isin_uses_gleif_isin_filter():
    f = _Fetcher({"filter%5Bisin%5D": (FIX / "gleif_isin_sap.json").read_text(),
                  "/isins": json.dumps({"data": []})})
    [e] = resolve_entities([{"isin": "DE0007164600"}], fetcher=f)
    assert e.lei == "529900D6BF99LW9R2E68"
    assert e.name == "SAP SE"
    assert e.country == "DE"
    assert e.resolution == "isin"
    # The ISIN we resolved by is always carried (seed), even if GLEIF lists none extra.
    assert "DE0007164600" in e.isins


def test_resolve_by_lei_populates_isins_from_gleif():
    # Route the LEI record AND the LEI->isins relationship (order matters: /isins first
    # so it isn't shadowed by the broader lei-records/<lei> route).
    f = _Fetcher({
        "/isins": (FIX / "gleif_isins_abinbev.json").read_text(),
        "lei-records/5493008H3828EMEXB082": (FIX / "gleif_lei_sap.json").read_text(),
    })
    [e] = resolve_entities([{"lei": "5493008H3828EMEXB082"}], fetcher=f)
    assert len(e.isins) == 24, "all of the issuer's ISINs should be carried"
    assert "BE0974293251" in e.isins, "the equity ISIN (STORI search key) must be present"


def test_populate_isins_false_skips_the_extra_call():
    f = _Fetcher({"lei-records/5493008H3828EMEXB082": (FIX / "gleif_lei_sap.json").read_text()})
    [e] = resolve_entities([{"lei": "5493008H3828EMEXB082"}], fetcher=f, populate_isins=False)
    assert e.isins == ()
    assert not any("/isins" in c for c in f.calls), "no LEI->isins call when disabled"


def test_isin_population_caps_the_count(monkeypatch):
    import company_corpus.eu.entities as ent
    monkeypatch.setattr(ent, "_ISIN_CAP", 5)
    f = _Fetcher({
        "/isins": (FIX / "gleif_isins_abinbev.json").read_text(),
        "lei-records/X": (FIX / "gleif_lei_sap.json").read_text(),
    })
    [e] = resolve_entities([{"lei": "X"}], fetcher=f)
    assert len(e.isins) == 5


# ---------------------------------------------------------------------------
# OpenFIGI bridge: GLEIF ISIN->LEI mapping misses the ISIN
# ---------------------------------------------------------------------------

from company_corpus.openfigi import OPENFIGI_URL  # noqa: E402


def _gleif_rec(lei, name, country):
    return {"attributes": {"lei": lei, "entity": {
        "legalName": {"name": name}, "legalAddress": {"country": country}}}}


class _BridgeFetcher:
    """GLEIF ISIN filter is empty; OpenFIGI returns a name; GLEIF fulltext returns
    a noise record plus the real one. Records the OpenFIGI call + fulltext query."""

    def __init__(self, *, figi_name, fulltext_rows):
        self._name = figi_name
        self._rows = fulltext_rows
        self.figi_called = False
        self.fulltext_url = None

    def get_json(self, url, **_):
        if "filter%5Bisin%5D" in url:
            return {"data": []}             # GLEIF has no ISIN->LEI mapping
        if "fulltext" in url:
            self.fulltext_url = url
            return {"data": self._rows}
        if "/isins" in url:
            return {"data": []}
        raise RuntimeError(f"unexpected get_json {url}")

    def post_json(self, url, body, **_):
        self.figi_called = True
        assert url == OPENFIGI_URL
        assert body == [{"idType": "ID_ISIN", "idValue": "IE00BF2NR112"}]
        return [{"data": [{"name": self._name}]}] if self._name else [{"data": []}]


def test_isin_miss_bridges_via_openfigi_to_single_gleif_match():
    """GLEIF ISIN miss -> OpenFIGI name -> GLEIF fulltext; the one record whose
    normalised legal name matches binds the LEI (resolution='isin-figi')."""
    f = _BridgeFetcher(
        figi_name="GREENCOAT RENEWABLES PLC",
        fulltext_rows=[
            _gleif_rec("NOISE", "Greencoat Capital LLP", "GB"),               # != normalised
            _gleif_rec("GREENLEI", "Greencoat Renewables Public Limited Company", "IE"),
        ],
    )
    [e] = resolve_entities([{"isin": "IE00BF2NR112"}], fetcher=f, populate_isins=False)
    assert f.figi_called
    assert e.lei == "GREENLEI" and e.resolution == "isin-figi" and e.country == "IE"
    assert "IE00BF2NR112" in e.isins  # the queried ISIN is seeded
    assert "PLC" not in f.fulltext_url  # GLEIF queried by the *core* name


def test_bridge_is_no_guess_on_ambiguous_normalised_match():
    """Two records normalising to the same core -> never bind."""
    f = _BridgeFetcher(
        figi_name="GREENCOAT RENEWABLES PLC",
        fulltext_rows=[
            _gleif_rec("A", "Greencoat Renewables PLC", "IE"),
            _gleif_rec("B", "Greencoat Renewables Limited", "GB"),  # also -> 'greencoat renewables'
        ],
    )
    [e] = resolve_entities([{"isin": "IE00BF2NR112"}], fetcher=f, populate_isins=False)
    assert e.lei is None and e.resolution == "unresolved"


def test_bridge_unresolved_when_openfigi_has_no_name():
    f = _BridgeFetcher(figi_name=None, fulltext_rows=[])
    [e] = resolve_entities([{"isin": "IE00BF2NR112"}], fetcher=f, populate_isins=False)
    assert e.lei is None and e.resolution == "unresolved"
