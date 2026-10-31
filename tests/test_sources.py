from pathlib import Path

from src.fetchers import rss
from src.fetchers.rss import parse_feed
from src.pipeline import check_sources, load_config

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures"


def hub_source():
    cfg = load_config(ROOT / "config" / "sources.yaml")
    return next(s for s in cfg["sources"] if s["id"] == "bis_research_hub"), cfg


def test_config_covers_required_institution_groups():
    _, cfg = hub_source()
    enabled = [s for s in cfg["sources"] if s.get("enabled")]
    fed = [s for s in enabled if s.get("group") == "Federal Reserve System"]
    assert len(fed) == 13                                   # Board + 12 regional banks
    g10 = {i["name"] for i in cfg["institutions"] if i.get("group") == "G10 central banks and ECB"}
    for bank in ["European Central Bank", "Bank of Japan", "Bank of England", "Bank of Canada",
                 "Deutsche Bundesbank", "Banque de France", "Banca d'Italia", "De Nederlandsche Bank",
                 "National Bank of Belgium", "Sveriges Riksbank", "Swiss National Bank"]:
        assert bank in g10
    assert {"aer", "ecta", "jpe", "qje", "restud", "nber", "bpea", "imf", "bis"} <= {s["id"] for s in enabled}


def test_aggregated_feed_attributes_each_item_to_its_central_bank():
    src, _ = hub_source()
    papers = parse_feed((FIX / "reshub_sample.xml").read_bytes(), src)
    by_title = {p.title: (p.source_id, p.source_name) for p in papers}
    assert by_title["Monetary policy and euro area bank lending"] == ("ecb", "European Central Bank")
    assert by_title["Inflation expectations of Japanese firms"] == ("boj", "Bank of Japan")
    assert by_title["Bank of Canada staff paper via RePEc"] == ("boc", "Bank of Canada")
    assert "A FEDS paper also in Fed in Print" not in by_title            # covered by Fed in Print
    assert by_title["A paper from an unmapped bank"][0] == "other:newbank.example.org"


def test_check_sources_reports_without_storing(monkeypatch):
    src, cfg = hub_source()
    cfg = {**cfg, "sources": [src, {"id": "broken", "type": "rss", "url": "x", "name": "B", "enabled": True}]}

    def fake_download(url, settings, params=None):
        if url == "x":
            raise rss.FetchError("HTTP 404")
        return (FIX / "reshub_sample.xml").read_bytes()

    monkeypatch.setattr(rss, "download", fake_download)
    rows = {r["id"]: r for r in check_sources(cfg)}
    assert rows["bis_research_hub"]["ok"] and rows["bis_research_hub"]["n"] == 4
    assert dict(rows["bis_research_hub"]["institutions"])["Bank of Japan"] == 1
    assert not rows["broken"]["ok"] and "404" in rows["broken"]["error"]
