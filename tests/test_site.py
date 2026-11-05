import json
import re

import pytest

from src import analysis
from src.issues import issue_id_and_title, publish_issue
from src.site import build_site
from tests.test_analysis import ABSTRACT, CFG, FakeClient, make_store

SOURCES = {"sources": [{"id": s, "name": s.upper() + " full name", "category": "journal", "enabled": True}
                       for s in ("nber", "imf", "bis")]}
SITE = {"title": "The Research Roundup", "tagline": "t", "site_url": "https://example.org/rr/"}


def test_issue_named_after_window_midpoint():
    assert issue_id_and_title("2026-10-01T00:00:00+00:00", "2026-11-01T00:00:00+00:00") == ("2026-10", "October 2026")
    assert issue_id_and_title("2026-09-30T12:00:00+00:00", "2026-10-31T12:00:00+00:00")[0] == "2026-10"


@pytest.fixture
def built(tmp_path):
    store = make_store(tmp_path, [
        ("nber", "Rate shocks [DM] score=9", ABSTRACT),
        ("imf", "EM capital flows </script><b>x</b> [EM] score=8", ABSTRACT),
        ("bis", "Global liquidity [Transversal] score=7", ABSTRACT),
        ("nber", "No abstract [DM] score=9", ""),
    ])
    report = analysis.run_analysis(store, FakeClient(), CFG)
    publish_issue(store, report, client=None)
    out = tmp_path / "site"
    res = build_site(store, SOURCES, SITE, out_dir=out)
    return store, report, out, res


def test_site_files_and_newsletter_content(built):
    store, report, out, res = built
    issue_id = res["issues"][0]
    newsletter = re.sub(r"\s+", " ", (out / "issues" / f"{issue_id}.html").read_text())
    assert (out / "index.html").exists() and (out / ".nojekyll").exists()
    for e in report["entries"]:
        assert e["analysis"]["summary"] in newsletter
    assert "Sources that needed a fallback" in newsletter and "Title only" in newsletter
    assert "&lt;/script&gt;" in newsletter                       # titles are escaped
    assert 'href="https://example.org/rr/"' in newsletter        # absolute link for email


def test_dashboard_embeds_valid_json_safely(built):
    _, _, out, _ = built
    html = (out / "index.html").read_text()
    assert html.count("</script>") == 2                           # no early script close from data
    raw = re.search(r'<script type="application/json" id="site-data">(.*?)</script>', html, re.S).group(1)
    data = json.loads(raw.replace("<\\/", "</"))
    assert len(data["issues"]) == 1 and len(data["papers"]) == 4
    featured = [p for p in data["papers"] if p["issues"]]
    assert all(p["analysis"] for p in featured)
    assert {p["status"] for p in data["papers"]} == {"scored", "title_only"}


def test_republishing_same_month_replaces_issue(built):
    store, report, _, _ = built
    publish_issue(store, report, client=None)
    assert len(store.get_issues()) == 1


def test_editor_note_uses_llm_and_falls_back(built):
    store, report, _, _ = built

    class NoteClient:
        model = "m"
        def generate_json(self, prompt, system=""):
            return {"intro": "A focused note."}

    assert publish_issue(store, report, client=NoteClient())["intro"] == "A focused note."
    assert "This issue features" in publish_issue(store, report, client=None)["intro"]


def test_issue_bar_links_recent_issues_newest_first():
    from src.site import issue_nav
    data = {"issues": [{"id": f"2026-{m:02d}"} for m in range(1, 13)]}
    site = {"site_url": "https://x.io/rr/"}
    nav = issue_nav(data, {"id": "2026-12"}, site)
    assert [n["id"] for n in nav] == ["2026-12", "2026-11", "2026-10", "2026-09", "2026-08", "2026-07"]
    assert nav[0]["current"] and nav[1]["url"] == "https://x.io/rr/issues/2026-11.html"
    old = issue_nav(data, {"id": "2026-02"}, site)          # an old issue still shows itself
    assert old[-1]["id"] == "2026-02" and old[-1]["current"] and len(old) == 6
