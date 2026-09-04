from company_corpus import cli


def test_eu_financials_subcommand_calls_producer(monkeypatch, tmp_path):
    captured = {}

    def fake_build(specs, *, fetcher, config, write, use_arelle=False):
        captured["specs"] = specs
        captured["write"] = write
        captured["use_arelle"] = use_arelle
        return {"entities": 1, "with_financials": 1, "periods": 3,
                "coverage_path": str(tmp_path / "cov.jsonl"), "paths": []}

    monkeypatch.setattr(cli, "build_eu_financials", fake_build)
    parser = cli.build_parser()
    args = parser.parse_args(["eu-financials", "--leis", "LEI1,LEI2", "--write"])
    rc = args.func(args)
    assert rc == 0
    assert captured["specs"] == [{"lei": "LEI1"}, {"lei": "LEI2"}]
    assert captured["write"] is True
    assert captured["use_arelle"] is False


def test_eu_financials_arelle_flag_flows_through(monkeypatch, tmp_path):
    captured = {}

    def fake_build(specs, *, fetcher, config, write, use_arelle=False):
        captured["use_arelle"] = use_arelle
        return {"entities": 1, "with_financials": 1, "periods": 2,
                "coverage_path": None, "paths": []}

    monkeypatch.setattr(cli, "build_eu_financials", fake_build)
    parser = cli.build_parser()
    args = parser.parse_args(["eu-financials", "--leis", "LEI1", "--arelle"])
    rc = args.func(args)
    assert rc == 0
    assert captured["use_arelle"] is True
