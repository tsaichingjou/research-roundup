from src import analysis, llm
from tests.test_analysis import CFG, FakeClient, make_store


class Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text, self.headers = status, payload, text, {}

    def json(self):
        return self._payload


OK = {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
      "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2}}


def make_client(monkeypatch, answers):
    calls = []

    def post(url, **kw):
        model = url.split("/models/")[1].split(":")[0]
        calls.append(model)
        return answers[model]()
    monkeypatch.setattr(llm.requests, "post", post)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    return llm.GeminiClient(["strong", "lite"], min_interval_seconds=0), calls


def test_daily_quota_switches_model_and_is_remembered(monkeypatch):
    quota = Resp(429, text='{"error": {"details": [{"retryDelay": "53773s"}]}}')
    client, calls = make_client(monkeypatch, {"strong": lambda: quota, "lite": lambda: Resp(200, OK)})
    assert client.generate_json("x") == {"ok": True}
    client.prefer_first()                     # next write-up: quota-exhausted model stays skipped
    assert client.model == "lite"
    client.generate_json("y")
    assert calls == ["strong", "lite", "lite"]


def test_short_rate_limit_is_waited_out(monkeypatch):
    answers = iter([Resp(429, text='{"retryDelay": "5s"}'), Resp(200, OK)])
    client, calls = make_client(monkeypatch, {"strong": lambda: next(answers), "lite": lambda: Resp(200, OK)})
    client.generate_json("x")
    assert calls == ["strong", "strong"]


def test_rewrite_keeps_previous_write_up_when_llm_fails(tmp_path):
    store = make_store(tmp_path, [("bis", "Rates [DM] score=9", "We study rates. " * 10)])
    analysis.run_analysis(store, FakeClient(), CFG)

    class Broken(FakeClient):
        def generate_json(self, prompt, system=""):
            if prompt.startswith("Screen"):
                return super().generate_json(prompt, system)
            raise llm.LLMError("quota")
    report = analysis.run_analysis(store, Broken(), CFG, rewrite=True)
    assert len(report["entries"]) == 1 and report["entries"][0]["analysis"]["summary"] == "A summary."
