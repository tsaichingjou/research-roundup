import io

from src import analysis, enrich, fulltext
from src.storage import Storage
from tests.test_analysis import CFG, FakeClient, make_store

TITLE = "Offshore Forwards and Exchange Rate Transmission in Emerging Markets"
BODY = ("Offshore forwards and exchange rate transmission in emerging markets are studied using "
        "transaction-level data. We find that a one standard deviation shock to offshore "
        "non-deliverable forward rates moves onshore spot rates by 0.4 percent within a day. ") * 30


def make_pdf(text: str) -> bytes:
    """A minimal text PDF, written by hand so the tests need no PDF library."""
    lines = [text[i:i + 90] for i in range(0, len(text), 90)]
    pages = [lines[i:i + 50] for i in range(0, len(lines), 50)]
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None,
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for page in pages:
        body = "BT /F1 10 Tf 40 800 Td 14 TL " + " ".join(
            "(" + ln.replace("\\", "").replace("(", "").replace(")", "") + ") '" for ln in page) + " ET"
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


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


def test_papers_without_abstract_get_title_label_but_are_never_selected(tmp_path):
    store = make_store(tmp_path, [("jpe", "Labor Markets and Immigration [EM] score=10", ""),
                                  ("aer", "Rates [DM] score=8", "We study rates and spreads. " * 10)])
    report = analysis.run_analysis(store, FakeClient(), CFG)
    st = {s["uid"]: s for s in store.get_screening().values()}
    title_only = [s for s in st.values() if s["status"] == "title_only"]
    assert len(title_only) == 1 and title_only[0]["market_focus"] == "EM" and title_only[0]["score"] is None
    assert [e["paper"]["source_id"] for e in report["entries"]] == ["aer"]
    assert report["coverage"]["jpe"]["classified from title"] == 1


def test_title_only_paper_is_rescored_once_abstract_recovered(tmp_path):
    store = make_store(tmp_path, [("jpe", "Rates Abroad [EM] score=9", "")])
    analysis.run_screening(store, FakeClient(), CFG)
    uid = store.all_papers()[0]["uid"]
    assert store.get_screening()[uid]["status"] == "title_only"
    with store._conn() as conn:
        conn.execute("UPDATE papers SET abstract=? WHERE uid=?", ("We study rates abroad. " * 10, uid))
    analysis.run_screening(store, FakeClient(), CFG)
    assert store.get_screening()[uid]["status"] == "scored" and store.get_screening()[uid]["score"] == 9
