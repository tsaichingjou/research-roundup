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


def test_month_just_ended_stays_open_until_publish_day(tmp_path):
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05"), ("b", "Nov [DM] score=7", "2026-11-02")])
    analysis.run_screening(s, FakeClient(), CFG, now=d("2026-11-03"))
    cfg = {**CFG, "schedule": {"publish_day": 5}}
    ids = [(p["id"], p["closing"]) for p in analysis.provisional_periods(s, cfg, now=d("2026-11-03"))]
    assert ids == [("2026-11", False), ("2026-10", True)]
    assert [p["id"] for p in analysis.provisional_periods(s, cfg, now=d("2026-11-06"))] == ["2026-11"]


def test_paper_indexed_before_publish_day_joins_its_own_month(tmp_path):
    from src.issues import publish_issue
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05")])
    publish_issue(s, analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-02")))   # an early version
    s.save_papers([Paper(source_id="b", source_name="b", title="Oct late [EM] score=9",
                         url="https://x.org/l", abstract=ABSTRACT, published=d("2026-10-29"))])
    report = analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-05"))           # the real issue
    assert {e["paper"]["title"] for e in report["entries"]} == {"Oct [DM] score=8", "Oct late [EM] score=9"}


def test_write_up_by_fallback_model_is_retried_with_preferred(tmp_path):
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05")])

    class Writer(FakeClient):
        candidates = ["strong", "lite"]

        def __init__(self, model):
            super().__init__()
            self.model = model
    analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-05"), analysis_client=Writer("lite"))
    uid = s.all_papers()[0]["uid"]
    assert s.get_analysis(uid)["model"] == "lite"
    w = Writer("strong")
    analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-05"), analysis_client=w)
    assert s.get_analysis(uid)["model"] == "strong" and any(p.startswith("Write") or "Title:" in p for p in w.prompts)


def test_catch_up_remakes_missing_early_or_fallback_issues(tmp_path):
    from src.issues import issue_needs_publishing, publish_issue
    cfg = {**CFG, "schedule": {"publish_day": 5}, "llm": {"analysis_models": ["strong", "lite"]}}
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05")])
    assert issue_needs_publishing(s, cfg, now=d("2026-11-03")) is None          # before the 5th
    assert issue_needs_publishing(s, cfg, now=d("2026-11-06")) == ("2026-10", "not published yet")
    assert issue_needs_publishing(s, cfg, now=d("2026-11-10")) is None          # grace period over

    class Writer(FakeClient):
        def __init__(self, model):
            super().__init__()
            self.model = model
    early = analysis.run_analysis(s, FakeClient(), cfg, now=d("2026-11-03"), analysis_client=Writer("strong"))
    publish_issue(s, early)
    with s._conn() as c:                                                       # made on 3 Nov
        c.execute("UPDATE issues SET published_at='2026-11-03T12:00:00+00:00'")
    assert issue_needs_publishing(s, cfg, now=d("2026-11-05"))[1] == "made before the publish day"
    with s._conn() as c:
        c.execute("UPDATE issues SET published_at='2026-11-05T12:00:00+00:00'")
    assert issue_needs_publishing(s, cfg, now=d("2026-11-06")) is None
    with s._conn() as c:
        c.execute("UPDATE analysis SET model='lite'")
    assert "fallback" in issue_needs_publishing(s, cfg, now=d("2026-11-06"))[1]


def test_drafts_on_the_day_before_and_reused_on_publish_day(tmp_path):
    from src.issues import publish_issue
    cfg = {**CFG, "schedule": {"publish_day": 5}}
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05"), ("b", "Oct EM [EM] score=7", "2026-10-20")])
    assert analysis.draft_month(s, cfg, now=d("2026-11-03")) is None
    assert analysis.draft_month(s, cfg, now=d("2026-11-04")) == "2026-10"

    class Writer(FakeClient):
        model = "strong"
        candidates = ["strong", "lite"]
    w1 = Writer()
    analysis.run_analysis(s, FakeClient(), cfg, now=d("2026-11-04"), analysis_client=w1, month="2026-10")
    drafted = sum("Title:" in p for p in w1.prompts)
    assert drafted == 2 and s.get_issue("2026-10") is None              # drafted, not published
    # a paper that arrives on the 4th enters the selection on the 5th; only it is written up
    s.save_papers([Paper(source_id="c", source_name="c", title="Late [DM] score=9", url="https://x.org/c",
                         abstract=ABSTRACT, published=d("2026-10-30"))])
    w2 = Writer()
    publish_issue(s, analysis.run_analysis(s, FakeClient(), cfg, now=d("2026-11-05"), analysis_client=w2))
    assert sum("Title:" in p for p in w2.prompts) == 1
    assert len(s.get_issue("2026-10")["paper_uids"]) == 3
    assert analysis.draft_month(s, cfg, now=d("2026-12-04")) == "2026-11"
