import io

from src import analysis, enrich, fulltext
from src.storage import Storage
from tests.test_analysis import CFG, FakeClient, make_store

TITLE = "Offshore Forwards and Exchange Rate Transmission in Emerging Markets"
BODY = ("Offshore forwards and exchange rate transmission in emerging markets are studied using "
        "transaction-level data. We find that a one standard deviation shock to offshore "
        "non-deliverable forward rates moves onshore spot rates by 0.4 percent within a day. ") * 30


def make_pdf(text: str) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in [text[i:i + 90] for i in range(0, len(text), 90)]:
        c.drawString(40, y, line)
        y -= 14
        if y < 40:
            c.showPage()
            y = 800
    c.save()
    return buf.getvalue()


class Resp:
    def __init__(self, body: bytes, ctype: str, url: str, status: int = 200):
        self.content, self.status_code, self.url = body, status, url
        self.headers = {"Content-Type": ctype}

    def iter_content(self, n):
        yield self.content

    def json(self):
        import json
        return json.loads(self.content)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_get(routes):
    def get(url, **kw):
        for key, (body, ctype, *status) in routes.items():
            if key in url:
                return Resp(body, ctype, url, status[0] if status else 200)
        return Resp(b"not found", "text/html", url, 404)
    return get


def test_html_text_keeps_paragraphs_and_finds_pdf_link():
    html = (f'<html><head><meta name="citation_pdf_url" content="/paper.pdf"></head><body>'
            f'<nav><p>Home About Research Publications Contact us today</p></nav>'
            f'<h1>{TITLE}</h1><p>{BODY[:300]}</p><script>var x = "ignore me please, long script";</script>'
            f'<footer><p>Copyright notice and privacy policy for this website</p></footer></body></html>')
    text, pdf = fulltext.html_text(html)
    assert pdf == "/paper.pdf"
    assert TITLE in text and "Offshore forwards" in text
    assert "Home About" not in text and "ignore me" not in text and "Copyright" not in text


def test_trim_drops_references_and_caps_length():
    text = "Intro. " * 500 + "\nReferences\n" + "Smith (2020). " * 200
    out = fulltext.trim(text, 100_000)
    assert "Smith (2020)" not in out
    assert len(fulltext.trim("x" * 5000, 1000)) < 1100


def test_opening_prefers_abstract_section():
    text = f"{TITLE}\nJane Doe, BIS\nAbstract: We find that offshore forwards lead onshore rates. " + "More. " * 400
    assert fulltext.opening(text, 200).startswith("We find that offshore forwards")


def test_finder_follows_declared_pdf(monkeypatch):
    page = f'<html><head><meta name="citation_pdf_url" content="/wp.pdf"></head><body><p>{TITLE}</p></body></html>'
    monkeypatch.setattr(fulltext.requests, "get", fake_get({
        "bis.org/publ/work": (page.encode(), "text/html"),
        "bis.org/wp.pdf": (make_pdf(TITLE + ". " + BODY), "application/pdf"),
    }))
    found = fulltext.FullTextFinder({}).find({"url": "https://www.bis.org/publ/work1385.htm", "title": TITLE})
    assert found and found.kind == "PDF" and "0.4 percent" in found.text


def test_finder_skips_blocked_pages_and_datasets(monkeypatch):
    monkeypatch.setattr(fulltext.requests, "get", fake_get({
        "doi.org/10.1086": (b"<html>Are you a robot? Please complete the captcha</html>", "text/html"),
        "openalex.org": (b'{"best_oa_location": null}', "application/json"),
    }))
    finder = fulltext.FullTextFinder({})
    assert finder.find({"url": "https://doi.org/10.1086/744577", "title": TITLE}) is None
    assert finder.find({"url": "https://doi.org/10.5281/zenodo.1", "title": "Replication package"}) is None


def test_finder_rejects_page_about_something_else(monkeypatch):
    monkeypatch.setattr(fulltext.requests, "get", fake_get({
        "example.org": (f"<html><body><p>{'Cookie settings and site map for our visitors. ' * 80}</p></body></html>".encode(),
                        "text/html"),
    }))
    assert fulltext.FullTextFinder({}).find({"url": "https://example.org/x", "title": TITLE}) is None


def test_enricher_uses_opening_of_text(monkeypatch):
    monkeypatch.setattr(fulltext.requests, "get", fake_get({
        "fedinprint.org/item/x/1": (f"<html><body><h1>{TITLE}</h1><p>{BODY}</p></body></html>".encode(), "text/html"),
    }))
    e = enrich.Enricher({}, min_chars=200, use_openalex=False, use_landing_page=False)
    text, origin = e.enrich({"uid": "u", "url": "https://fedinprint.org/item/x/1", "title": TITLE})
    assert origin == "opening of the text" and 200 <= len(text) <= 1500


class FullTextFinderFake:
    def __init__(self, available):
        self.available = available

    def find(self, paper):
        if paper["source_id"] in self.available:
            return fulltext.FullText(text="Full text with the estimate of 0.4 percent.", url="https://x/p.pdf", kind="PDF")
        return None


def test_write_ups_use_full_text_when_available(tmp_path):
    abstract = "We study how monetary policy shocks transmit to sovereign spreads. " * 4
    store = make_store(tmp_path, [("bis", "Rates [DM] score=9", abstract), ("aer", "Prices [DM] score=8", abstract)])
    client = FakeClient()
    report = analysis.run_analysis(store, client, CFG, fulltext_finder=FullTextFinderFake({"bis"}))
    basis = {e["paper"]["source_id"]: e["analysis"]["basis"] for e in report["entries"]}
    assert basis == {"bis": "full text", "aer": "abstract"}
    assert any("0.4 percent" in p for p in client.prompts)
    assert store.get_analysis(report["entries"][0]["paper"]["uid"])["full_text_kind"] == "PDF"


def test_datasets_are_excluded_and_old_exclusions_rechecked(tmp_path):
    real = analysis.load_analysis_config("config/analysis.yaml")
    cfg = {**CFG, "prefilter": real["prefilter"]}            # the shipped title rules
    store = make_store(tmp_path, [("ecb", "Replication Data for: Debt Sustainability [DM] score=5", "")])
    analysis.run_screening(store, FakeClient(), cfg)
    assert list(store.get_screening().values())[0]["reason"] == "not a research paper"

    # a dataset excluded earlier as "no abstract" is relabelled on the next run
    s = Storage(tmp_path / "c.db")
    from src.models import Paper
    s.save_papers([Paper(source_id="boj", source_name="BOJ", title="Cyclicality [DM]",
                         url="https://doi.org/10.5281/zenodo.22154022", abstract="")])
    uid = s.all_papers()[0]["uid"]
    s.save_screening([{"uid": uid, "status": "excluded", "reason": "no abstract available"}], model=None)
    analysis.run_screening(s, FakeClient(), CFG)
    assert s.get_screening()[uid]["reason"] == "dataset, not a paper"


def test_finder_follows_linked_pdf_on_short_landing_page(monkeypatch):
    page = (f'<html><body><h1>{TITLE}</h1><p>{BODY[:600]}</p>'
            f'<a href="https://other.org/ad.pdf">Brochure</a><a href="/publ/work1383.pdf">Full text</a></body></html>')
    monkeypatch.setattr(fulltext.requests, "get", fake_get({
        "bis.org/publications/": (page.encode(), "text/html"),
        "bis.org/publ/work1383.pdf": (make_pdf(TITLE + ". " + BODY), "application/pdf"),
    }))
    found = fulltext.FullTextFinder({}).find({"url": "https://www.bis.org/publications/working-paper-1383", "title": TITLE})
    assert found and found.kind == "PDF" and found.url.endswith("work1383.pdf")
