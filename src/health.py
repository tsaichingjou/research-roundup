"""Daily health check: problems that do not crash a run but need a person.

Runs at the end of every daily and monthly run. Each problem found becomes an
alert; the workflow opens one GitHub issue per alert (GitHub emails it to the
repository owner), does not repeat it while it stays open, and closes it with a
comment once the problem is gone. Thresholds leave room for problems the agent
fixes itself (a busy day at Gemini, a source down for an afternoon), so an
email means something needs attention.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

AUTH_MARKER = Path("llm-auth-failed.txt")    # written by main.py on an invalid API key

UNSCORED_DAYS = 2        # papers waiting this long for a score
SOURCE_FAIL_RUNS = 3     # consecutive failed fetches of one source


def check(storage, cfg: dict, now: datetime | None = None) -> list[dict]:
    """Current alerts, each {"key", "title", "body"}. The title is the identity of
    the alert: the workflow uses it to avoid opening the same issue twice."""
    from src.analysis import month_bounds, month_of, previous_month, publish_day

    now = now or datetime.now(timezone.utc)
    alerts = []

    # 1. The Gemini API key is rejected: nothing can be scored or written.
    if AUTH_MARKER.exists():
        alerts.append({
            "key": "auth", "title": "Agent health: the Gemini API key was rejected",
            "body": "The last run could not use Gemini: the API key was rejected "
                    f"({AUTH_MARKER.read_text(encoding='utf-8').strip()[:300]}).\n\n"
                    "Nothing is scored or written until this is fixed. Create a new key at "
                    "https://aistudio.google.com/apikey and update the `GEMINI_API_KEY` secret "
                    "(Settings → Secrets and variables → Actions), then run the workflow once."})

    # 2. Papers waiting for a score for more than two days (Gemini down or out of quota).
    since = month_bounds(previous_month(month_of(now)))[0]
    screened = storage.get_screening()
    cutoff = (now - timedelta(days=UNSCORED_DAYS)).isoformat()
    waiting = [p for p in storage.candidates(since)
               if p["uid"] not in screened and p["fetched_at"] < cutoff]
    if waiting:
        oldest = min(p["fetched_at"] for p in waiting)[:10]
        alerts.append({
            "key": "unscored", "title": "Agent health: papers are waiting to be scored",
            "body": f"{len(waiting)} papers collected more than {UNSCORED_DAYS} days ago have no score yet "
                    f"(oldest collected {oldest}).\n\nThe screening model has failed on every daily run "
                    "since: usually Gemini overloaded or out of its daily free quota for days in a row. "
                    "Check the latest run's log in the Actions tab. The papers are scored automatically "
                    "once the model answers again."})

    # 3. A source failing on its last three fetches (site moved, feed URL changed).
    failing = []
    for source_id, runs in storage.recent_fetches(SOURCE_FAIL_RUNS).items():
        recent = runs and runs[0]["run_at"] >= (now - timedelta(days=2)).isoformat()   # still fetched
        if recent and len(runs) == SOURCE_FAIL_RUNS and all(r["status"] != "ok" for r in runs):
            failing.append((source_id, runs[0]["message"]))
    if failing:
        lines = "\n".join(f"- `{s}`: {m[:200]}" for s, m in sorted(failing))
        alerts.append({
            "key": "sources", "title": "Agent health: sources failing repeatedly",
            "body": f"These sources failed on each of their last {SOURCE_FAIL_RUNS} fetches:\n\n{lines}\n\n"
                    "Usually the site moved its feed or changed its address: update the source in "
                    "`config/sources.yaml`. Other sources are not affected."})

    # 4. Last month's issue still missing, or still written by a fallback model,
    #    after the catch-up window (the 5th to the 8th) has passed.
    day = publish_day(cfg)
    if now.day > day + 3:
        month = previous_month(month_of(now))
        issue = storage.get_issue(month)
        preferred = cfg["llm"]["analysis_models"][0]
        fallback = [u for u in (issue or {}).get("paper_uids", [])
                    if (storage.get_analysis(u) or {}).get("model") not in (None, preferred)]
        if issue is None or fallback:
            what = ("has not been published" if issue is None else
                    f"still has {len(fallback)} write-ups by a fallback model")
            alerts.append({
                "key": "issue", "title": f"Agent health: the {month} issue needs attention",
                "body": f"The {month} issue {what}, and the automatic catch-up (the {day}th to the "
                        f"{day + 3}th) is over.\n\nRun the workflow from the Actions tab with mode "
                        "*republish* once Gemini is available again."})
    return alerts
