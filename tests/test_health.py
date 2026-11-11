from datetime import datetime, timezone

from src import health
from src.models import Paper
from src.storage import Storage
from tests.test_analysis import ABSTRACT, CFG

CFG2 = {**CFG, "schedule": {"publish_day": 5}, "llm": {"analysis_models": ["strong", "lite"]}}


def keys(alerts):
    """Alert keys, leaving out the date-dependent issue check (tested on its own below)."""
    return [a["key"] for a in alerts if a["key"] != "issue"]


def d(s):
    return datetime.fromisoformat(s + "T12:00:00+00:00")


def test_no_alerts_on_a_healthy_store(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Storage(tmp_path / "h.db")
    assert health.check(s, CFG2, now=d("2026-11-04")) == []


def test_unscored_papers_alert_only_after_two_days(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Storage(tmp_path / "h.db")
    s.save_papers([Paper(source_id="a", source_name="A", title="T", url="https://x/1", abstract=ABSTRACT,
                         published=d("2026-11-01"))])
    now = datetime.now(timezone.utc)
    assert keys(health.check(s, CFG2, now=now)) == []                 # just collected
    with s._conn() as c:
        c.execute("UPDATE papers SET fetched_at='2000-01-01T00:00:00+00:00'")
        c.execute("UPDATE papers SET published=?", (now.strftime("%Y-%m-%d"),))
    assert keys(health.check(s, CFG2, now=now)) == ["unscored"]


def test_source_failing_three_runs_and_auth_marker(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Storage(tmp_path / "h.db")
    for _ in range(3):
        s.log_fetch("fed_x", "error", message="HTTP 404")
    s.log_fetch("nber", "error", message="timeout")
    s.log_fetch("nber", "ok", 5, 1)
    now = datetime.now(timezone.utc)
    alerts = {a["key"]: a for a in health.check(s, CFG2, now=now) if a["key"] != "issue"}
    assert set(alerts) == {"sources"} and "fed_x" in alerts["sources"]["body"] and "nber" not in alerts["sources"]["body"]
    health.AUTH_MARKER.write_text("API key not valid")
    assert "auth" in {a["key"] for a in health.check(s, CFG2, now=now)}


def test_issue_alert_after_catch_up_window(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = Storage(tmp_path / "h.db")
    assert health.check(s, CFG2, now=d("2026-11-07")) == []           # catch-up still running
    alerts = health.check(s, CFG2, now=d("2026-11-09"))
    assert alerts[0]["title"] == "Agent health: the 2026-10 issue needs attention"
    assert "has not been published" in alerts[0]["body"]
