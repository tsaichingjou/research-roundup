from datetime import datetime, timezone

from src import analysis
from src.models import Paper
from src.storage import Storage
from tests.test_analysis import ABSTRACT, CFG, FakeClient


def d(s):
    return datetime.fromisoformat(s + "T12:00:00+00:00")


def store_with(tmp_path, specs):
    s = Storage(tmp_path / "p.db")
    s.save_papers([Paper(source_id=src, source_name=src, title=t, url=f"https://x.org/{i}",
                         abstract=ABSTRACT, published=d(pub) if pub else None)
                   for i, (src, t, pub) in enumerate(specs)])
    return s


def test_issue_covers_its_calendar_month_only(tmp_path):
    s = store_with(tmp_path, [("a", "Sep [DM] score=9", "2026-09-28"), ("b", "Oct [DM] score=8", "2026-10-02"),
                              ("c", "Oct end [EM] score=7", "2026-10-31"), ("d", "Nov [DM] score=9", "2026-11-01")])
    report = analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-03"))
    assert report["month"] == "2026-10"
    assert sorted(e["paper"]["title"] for e in report["entries"]) == ["Oct [DM] score=8", "Oct end [EM] score=7"]
    assert report["late_uids"] == []                    # no September issue: nothing is late


def test_late_arrival_joins_next_issue(tmp_path):
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05")])
    from src.issues import publish_issue
    publish_issue(s, analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-01")))
    # an October paper first seen after the October issue went out
    s.save_papers([Paper(source_id="b", source_name="b", title="Late Oct [EM] score=9",
                         url="https://x.org/late", abstract=ABSTRACT, published=d("2026-10-30"))])
    pool, late = analysis.month_pool(s, "2026-11")
    assert [p["title"] for p in pool] == ["Late Oct [EM] score=9"] and len(late) == 1
    analysis.run_screening(s, FakeClient(), CFG, now=d("2026-11-04"))
    prov = analysis.provisional(s, CFG, now=d("2026-11-04"))
    assert prov["id"] == "2026-11" and prov["title"] == "November 2026" and len(prov["paper_uids"]) == 1
    # the paper already in the October issue is not offered again
    assert analysis.month_pool(s, "2026-10")[0][0]["title"] == "Oct [DM] score=8"


def test_republish_keeps_month_fixed(tmp_path):
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05"), ("b", "Nov [DM] score=9", "2026-11-02")])
    r1 = analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-01"))
    r2 = analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-20"))
    assert r1["window_start"] == r2["window_start"] == "2026-10-01T00:00:00+00:00"
    assert [e["paper"]["title"] for e in r2["entries"]] == ["Oct [DM] score=8"]
