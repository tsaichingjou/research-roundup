import json

from src import analysis, enrich
from src.fetchers.http import FetchError
from src.models import Paper
from src.storage import Storage
from tests.test_analysis import ABSTRACT, CFG, LAST_MONTH, FakeClient

LONG = "We study how sovereign spreads in emerging markets react to U.S. monetary policy shocks. " * 4


def _inverted(text):
    inv = {}
    for i, w in enumerate(text.split()):
        inv.setdefault(w, []).append(i)
    return inv


def test_reconstruct_and_doi():
    inv = {"Rates": [0], "rise": [1], "when": [2], "rates": [4], "inflation": [3]}
    assert enrich.reconstruct_abstract(inv) == "Rates rise when inflation rates"
    assert enrich.doi_of("https://doi.org/10.1086/736512") == "10.1086/736512"
    assert enrich.doi_of("https://www.bis.org/publ/work1330.htm") is None


def test_abstract_from_html_prefers_citation_tag_and_rejects_generic():
    html = (f'<html><head><meta name="description" content="We use cookies on this website.">'
            f'<meta name="citation_abstract" content="Abstract: {LONG}"></head></html>')
    assert enrich.abstract_from_html(html, 200).startswith("We study how sovereign")
    assert enrich.abstract_from_html('<meta name="description" content="We use cookies ' + "x" * 300 + '">', 200) is None


def _fake_download(routes):
    def fake(url, settings, params=None):
        for key, value in routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value if isinstance(value, bytes) else json.dumps(value).encode()
        raise FetchError(f"{url} returned HTTP 404")
    return fake


def test_enricher_uses_openalex_then_landing_page(monkeypatch):
    inv = _inverted(LONG)
    monkeypatch.setattr(enrich, "download", _fake_download({
        "works/doi:10.1086/1": {"title": "Spreads and Shocks", "abstract_inverted_index": inv},
        "api.openalex.org/works": {"results": [{"title": "Something else", "abstract_inverted_index": inv}]},
        "bank.example/paper": f'<meta name="citation_abstract" content="{LONG}">'.encode(),
    }))
    e = enrich.Enricher({"contact_email": "a@b.c"})
    assert e.enrich({"uid": "1", "title": "Spreads and Shocks", "url": "https://doi.org/10.1086/1"})[1] == "OpenAlex"
    # title search returns a different paper -> rejected, falls back to the landing page
    text, origin = e.enrich({"uid": "2", "title": "Spreads and shocks", "url": "https://bank.example/paper"})
    assert origin == "publisher page" and text.startswith("We study")
    assert e.enrich({"uid": "3", "title": "Nothing", "url": "https://nowhere.example/x"}) is None


def test_recovered_paper_is_screened_next_run(tmp_path, monkeypatch):
    store = Storage(tmp_path / "t.db")
    store.save_papers([Paper(source_id="jpe", source_name="JPE", title="Spreads [EM] score=8",
                             url="https://doi.org/10.1086/9", abstract="", published=LAST_MONTH)])
    analysis.run_analysis(store, FakeClient(), CFG)
    assert next(iter(store.get_screening().values()))["status"] == "title_only"

    inv = _inverted(LONG)
    monkeypatch.setattr(enrich, "download", _fake_download(
        {"works/doi:10.1086/9": {"title": "Spreads [EM] score=8", "abstract_inverted_index": inv}}))
    summary = enrich.run_enrichment(store, {}, {**CFG, "enrichment": {}})
    assert summary["found"]["OpenAlex"] == 1
    assert store.get_screening() == {}                      # exclusion cleared

    report = analysis.run_analysis(store, FakeClient(), CFG)
    assert report["coverage"]["jpe"]["scored"] == 1
    assert report["coverage"]["jpe"]["abstract recovered"] == 1
    # a failed lookup is not retried immediately
    store.save_papers([Paper(source_id="jpe", source_name="JPE", title="Other", url="https://doi.org/10.1086/10")])
    assert enrich.run_enrichment(store, {}, {**CFG, "enrichment": {}})["tried"] == 1
    assert enrich.run_enrichment(store, {}, {**CFG, "enrichment": {}})["tried"] == 0
