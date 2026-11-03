import json
import re
from datetime import datetime, timezone

import pytest

from src import analysis
from src.llm import LLMError, parse_json
from src.models import Paper
from src.storage import Storage

CFG = {
    "window": {"days": 31},
    "prefilter": {"min_abstract_chars": 50,
                  "exclude_title_patterns": ["^comment on", "^reply to"]},
    "screening": {"batch_size": 3, "max_abstract_chars": 500},
    "selection": {"min_papers": 2, "max_papers": 4, "min_score": 6, "max_per_source": 2},
}
ABSTRACT = "We study how monetary policy shocks transmit to sovereign spreads and exchange rates. " * 2


class FakeClient:
    """Returns deterministic JSON. Scores come from a number embedded in the title
    ("score=7"); market focus from a tag ("[EM]"). Records every prompt."""

    model = "fake-model"

    def __init__(self, drop_ids=()):
        self.prompts = []
        self.drop_ids = set(drop_ids)

    def generate_json(self, prompt, system=""):
        self.prompts.append(prompt)
        if prompt.startswith("Screen"):
            out = []
            for pid, title in re.findall(r"id: (p\d+)\nsource: .*\ntitle: (.*)", prompt):
                if pid in self.drop_ids:
                    continue
                score = int(re.search(r"score=(\d+)", title).group(1))
                market = re.search(r"\[(EM|DM|Transversal)\]", title).group(1)
                out.append({"id": pid, "score": score, "market_focus": market.lower(),
                            "topic": "Monetary policy", "reason": "test"})
            return out
        if prompt.startswith("These publications have no abstract"):
            return [{"id": pid, "market_focus": (re.search(r"\[(EM|DM|Transversal)\]", t) or [0, "DM"])[1],
                     "topic": "Labour", "reason": "From the title."}
                    for pid, t in re.findall(r"id: (p\d+)\nsource: .*\ntitle: (.*)", prompt)]
        title = re.search(r"Title: (.*)", prompt).group(1)
        market = re.search(r"\[(EM|DM|Transversal)\]", title).group(1)
        return {"summary": "A summary.", "key_takeaways": ["a", "b", "c", "d"],
                "relevance": "Matters for rates.", "market_focus": market,
                "market_focus_rationale": "Because.", "regions": ["Brazil"],
                "topics": ["Monetary policy"], "confidence": "HIGH"}


def make_store(tmp_path, specs):
    store = Storage(tmp_path / "t.db")
    papers = [Paper(source_id=src, source_name=src.upper(), title=title,
                    url=f"https://example.org/{i}", abstract=abstract)
              for i, (src, title, abstract) in enumerate(specs)]
    store.save_papers(papers)
    return store


def test_parse_json_tolerates_fences_and_prose():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json('Here you go: [{"a": 1}] hope it helps') == [{"a": 1}]


def test_prefilter_excludes_missing_abstract_and_comments():
    papers = [
        {"uid": "1", "title": "Real paper", "abstract": ABSTRACT},
        {"uid": "2", "title": "Comment on X", "abstract": ABSTRACT},
        {"uid": "3", "title": "No abstract", "abstract": ""},
    ]
    kept, excluded = analysis.prefilter(papers, CFG)
    assert [p["uid"] for p in kept] == ["1"]
    assert {e["uid"]: e["reason"] for e in excluded} == {
        "2": "not a research paper", "3": "no abstract available"}


def test_select_respects_per_source_cap_and_threshold():
    scored = [
        {"uid": str(i), "source_id": src, "score": sc, "market_focus": mf}
        for i, (src, sc, mf) in enumerate([
            ("nber", 9, "DM"), ("nber", 9, "DM"), ("nber", 8, "DM"),
            ("bis", 7, "Transversal"), ("imf", 3, "EM"),
        ])
    ]
    chosen = analysis.select(scored, CFG)
    assert [p["uid"] for p in chosen] == ["0", "1", "3"]  # third NBER paper capped, score-3 below threshold


def test_select_swaps_in_qualifying_em_paper():
    scored = [
        {"uid": "a", "source_id": "nber", "score": 9, "market_focus": "DM"},
        {"uid": "b", "source_id": "bis", "score": 9, "market_focus": "DM"},
        {"uid": "c", "source_id": "aer", "score": 8, "market_focus": "DM"},
        {"uid": "d", "source_id": "qje", "score": 8, "market_focus": "DM"},
        {"uid": "e", "source_id": "imf", "score": 7, "market_focus": "EM"},
    ]
    chosen = analysis.select(scored, CFG)
    assert len(chosen) == 4
    assert "e" in [p["uid"] for p in chosen]


def test_select_relaxes_threshold_to_reach_minimum():
    scored = [{"uid": str(i), "source_id": f"s{i}", "score": 3, "market_focus": "DM"} for i in range(3)]
    assert len(analysis.select(scored, CFG)) == 3


def test_validate_analysis_normalises_fields():
    out = analysis._validate_analysis({
        "summary": "s", "key_takeaways": ["a", "b", "c", "d"], "relevance": "r",
        "market_focus": "emerging markets", "market_focus_rationale": "x", "confidence": "HIGH"})
    assert out["market_focus"] == "EM" and len(out["key_takeaways"]) == 3
    assert out["confidence"] == "high"
    with pytest.raises(ValueError):
        analysis._validate_analysis({"summary": "s", "market_focus": "Asia"})


def test_run_analysis_end_to_end_and_caching(tmp_path):
    store = make_store(tmp_path, [
        ("nber", "Rate shocks [DM] score=9", ABSTRACT),
        ("nber", "Fiscal rules [DM] score=8", ABSTRACT),
        ("nber", "Labour supply [DM] score=7", ABSTRACT),
        ("imf", "EM capital flows [EM] score=8", ABSTRACT),
        ("bis", "Global liquidity [Transversal] score=7", ABSTRACT),
        ("aer", "Health insurance [DM] score=2", ABSTRACT),
        ("qje", "No abstract here [DM] score=9", ""),
        ("aer", "Comment on Something [DM] score=9", ABSTRACT),
    ])
    client = FakeClient()
    report = analysis.run_analysis(store, client, CFG)

    titles = [e["paper"]["title"] for e in report["entries"]]
    assert titles[0] == "Rate shocks [DM] score=9"
    assert "EM capital flows [EM] score=8" in titles
    assert len(titles) == 4 and sum("nber" == e["paper"]["source_id"] for e in report["entries"]) == 2
    assert report["coverage"]["qje"] == {"candidates": 1, "classified from title": 1}
    assert report["entries"][0]["analysis"]["key_takeaways"] == ["a", "b", "c"]

    # Second run: nothing re-screened or re-analysed -> no new LLM calls.
    n_calls = len(client.prompts)
    analysis.run_analysis(store, client, CFG)
    assert len(client.prompts) == n_calls


def test_papers_dropped_by_model_are_retried_next_run(tmp_path):
    store = make_store(tmp_path, [("nber", f"Paper {i} [DM] score=7", ABSTRACT) for i in range(3)])
    analysis.run_analysis(store, FakeClient(drop_ids={"p2"}), CFG)
    assert sum(r["status"] == "scored" for r in store.get_screening().values()) == 2
    analysis.run_analysis(store, FakeClient(), CFG)
    assert sum(r["status"] == "scored" for r in store.get_screening().values()) == 3


def test_gemini_client_requires_key(monkeypatch):
    from src.llm import GeminiClient
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(LLMError):
        GeminiClient(models=["m"])


def test_gemini_client_falls_back_on_404(monkeypatch):
    from src import llm

    class Resp:
        def __init__(self, code, payload=None):
            self.status_code, self._payload, self.headers = code, payload, {}
            self.text = json.dumps(payload or {"error": "not found"})

        def json(self):
            return self._payload

    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if "old-model" in url:
            return Resp(404)
        return Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    client = llm.GeminiClient(models=["old-model", "new-model"], api_key="k", min_interval_seconds=0)
    assert client.generate_json("hi") == {"ok": True}
    assert client.model == "new-model" and len(calls) == 2


def test_invalid_key_stops_immediately(monkeypatch, tmp_path):
    from src import llm

    class Resp:
        status_code, headers = 400, {}
        text = '{"error": {"message": "API key not valid. Please pass a valid API key."}}'

    calls = []
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: calls.append(1) or Resp())
    store = make_store(tmp_path, [("nber", f"Paper {i} [DM] score=7", ABSTRACT) for i in range(7)])
    client = llm.GeminiClient(models=["m"], api_key="paste-your-key-here", min_interval_seconds=0)
    with pytest.raises(llm.LLMAuthError):
        analysis.run_analysis(store, client, CFG)
    assert len(calls) == 1          # did not try the remaining batches


def test_gemini_client_falls_back_after_repeated_timeouts(monkeypatch):
    from src import llm

    class Resp:
        status_code, headers = 200, {}
        text = ""

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '[{"ok": 1}]'}]}}]}

    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if "slow-model" in url:
            raise llm.requests.Timeout("slow")
        return Resp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    client = llm.GeminiClient(models=["slow-model", "fast-model"], api_key="k", min_interval_seconds=0)
    assert client.generate_json("hi") == [{"ok": 1}]
    assert client.model == "fast-model"
    assert sum("slow-model" in c for c in calls) == 2      # gave up after 2 timeouts


def test_gemini_client_falls_back_when_model_overloaded(monkeypatch):
    from src import llm

    class Resp:
        def __init__(self, code):
            self.status_code, self.headers, self.text = code, {}, "overloaded"

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": '{"ok": 1}'}]}}]}

    monkeypatch.setattr(llm.requests, "post",
                        lambda url, **k: Resp(503 if "busy-model" in url else 200))
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    client = llm.GeminiClient(models=["busy-model", "ok-model"], api_key="k", min_interval_seconds=0)
    assert client.generate_json("hi") == {"ok": 1}
    assert client.model == "ok-model"


def test_rewrite_regenerates_cached_analyses(tmp_path):
    store = make_store(tmp_path, [(f"s{i}", f"Paper {i} [DM] score=8", ABSTRACT) for i in range(3)])
    client = FakeClient()
    analysis.run_analysis(store, client, CFG)
    n = len(client.prompts)
    analysis.run_analysis(store, client, CFG, rewrite=True)
    assert len(client.prompts) == n + 3          # 3 write-ups regenerated, no re-screening


def test_weekly_screening_then_monthly_reuses_scores(tmp_path):
    store = make_store(tmp_path, [(f"s{i}", f"Paper {i} [DM] score=8", ABSTRACT) for i in range(3)]
                       + [("s9", "No abstract [DM] score=8", "")])
    client = FakeClient()
    r = analysis.run_screening(store, client, CFG)
    assert r == {"new": 4, "excluded": 1, "scored": 3, "unscored": 0, "title_only": 1}
    n = len(client.prompts)
    report = analysis.run_analysis(store, client, CFG)
    assert len(report["entries"]) == 3
    assert not any(p.startswith("Screen") for p in client.prompts[n:])   # no re-screening
