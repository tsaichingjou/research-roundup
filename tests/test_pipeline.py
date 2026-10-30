from pathlib import Path

import pytest

from src.fetchers import crossref, http, rss
from src.fetchers.rss import FetchError, parse_feed
from src.pipeline import run_fetch
from src.storage import Storage

FIX = Path(__file__).parent / "fixtures"
NBER = {"id": "nber", "name": "NBER", "type": "rss", "url": "x"}
BIS = {"id": "bis", "name": "BIS", "type": "rss", "url": "y"}


def test_parse_rss2_cleans_html_and_splits_authors():
    papers = parse_feed((FIX / "nber_sample.xml").read_bytes(), NBER)
    assert len(papers) == 2
    p = papers[0]
    assert p.abstract.startswith("We study how policy rate")  # HTML tags removed
    assert "<p>" not in p.abstract
    assert p.authors == ["Jane Doe", "John Smith"]
    assert p.published.year == 2026


def test_parse_rdf_feed():
    papers = parse_feed((FIX / "bis_sample.xml").read_bytes(), BIS)
    assert [p.title for p in papers][0] == "Passive investors and loan spreads"
    assert papers[0].authors == ["Konrad Adler"]


def test_broken_feed_raises():
    with pytest.raises(FetchError):
        parse_feed((FIX / "broken.xml").read_bytes(), BIS)


def test_dedup_by_url_and_title(tmp_path):
    store = Storage(tmp_path / "t.db")
    nber = parse_feed((FIX / "nber_sample.xml").read_bytes(), NBER)
    bis = parse_feed((FIX / "bis_sample.xml").read_bytes(), BIS)
    assert store.save_papers(nber) == 2
    assert store.save_papers(nber) == 0          # same URLs again
    assert store.save_papers(bis) == 1           # 1 new, 1 duplicate title
    assert store.count() == 3


def test_one_failing_source_does_not_stop_others(tmp_path, monkeypatch):
    files = {"x": FIX / "nber_sample.xml", "y": FIX / "broken.xml"}
    monkeypatch.setattr(rss, "download", lambda url, s: files[url].read_bytes())
    config = {"settings": {}, "sources": [NBER, BIS, {**BIS, "id": "off", "enabled": False}]}
    store = Storage(tmp_path / "t.db")
    results = {r.source_id: r for r in run_fetch(config, store)}
    assert results["nber"].ok and results["nber"].n_new == 2
    assert not results["bis"].ok
    assert "off" not in results


def test_authors_embedded_in_title_are_split():
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title>Tariff Staggering -- by Stephanie Schmitt-Groh\xc3\xa9, Mart\xc3\xadn Uribe</title>
    <link>https://www.nber.org/papers/w1</link><description>d</description></item>
    </channel></rss>"""
    p = parse_feed(feed, NBER)[0]
    assert p.title == "Tariff Staggering"
    assert p.authors == ["Stephanie Schmitt-Grohé", "Martín Uribe"]


def test_404_is_not_retried(monkeypatch):
    calls = []

    class Resp:
        status_code = 404

    monkeypatch.setattr(http.requests, "get", lambda *a, **k: calls.append(1) or Resp())
    with pytest.raises(FetchError):
        http.download("https://example.org/missing", {"max_retries": 3})
    assert len(calls) == 1


CROSSREF_SAMPLE = b"""{"status":"ok","message":{"items":[
 {"DOI":"10.5089/9798229028139.001","title":["Macroeconomic Effects of Forced Displacement in Sub-Saharan Africa"],
  "author":[{"given":"Ana","family":"Lopez"},{"given":"Ben","family":"Kim"}],
  "abstract":"<jats:p>We estimate the <jats:italic>growth</jats:italic> effects of displacement.</jats:p>",
  "published":{"date-parts":[[2026,10]]}},
 {"DOI":"10.3982/ECTA0000","title":["Front Matter"],"published":{"date-parts":[[2026,9,1]]}},
 {"DOI":"10.1093/qje/qjae001","title":["Missing abstract paper"],"published-online":{"date-parts":[[2026,10,15]]}}
]}}"""


def test_crossref_parse_filters_front_matter_and_cleans_abstract():
    src = {"id": "imf", "name": "IMF"}
    papers = crossref.parse_response(CROSSREF_SAMPLE, src)
    assert [p.title for p in papers] == [
        "Macroeconomic Effects of Forced Displacement in Sub-Saharan Africa",
        "Missing abstract paper",
    ]
    p = papers[0]
    assert p.url == "https://doi.org/10.5089/9798229028139.001"
    assert p.authors == ["Ana Lopez", "Ben Kim"]
    assert p.abstract == "We estimate the growth effects of displacement."
    assert (p.published.year, p.published.month, p.published.day) == (2026, 10, 1)
    assert papers[1].published.day == 15


def test_crossref_bad_json_raises():
    with pytest.raises(FetchError):
        crossref.parse_response(b"<html>oops</html>", {"id": "x", "name": "X"})


def test_crossref_year_only_date_falls_back_to_created():
    sample = b"""{"message":{"items":[
     {"DOI":"10.3982/ECTA1","title":["The Complexity of Multidimensional Learning"],
      "published":{"date-parts":[[2026]]},"created":{"date-parts":[[2026,10,20]]}},
     {"DOI":"10.3982/ECTA2","title":["Submission of Manuscripts to the Econometric Society"],
      "created":{"date-parts":[[2026,10,20]]}}
    ]}}"""
    papers = crossref.parse_response(sample, {"id": "ecta", "name": "Econometrica"})
    assert len(papers) == 1                                  # editorial note filtered out
    assert (papers[0].published.month, papers[0].published.day) == (10, 20)
