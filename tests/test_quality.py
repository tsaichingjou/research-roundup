from pathlib import Path

from src import analysis, quality
from src.issues import publish_issue
from tests.test_analysis import CFG, FakeClient
from tests.test_periods import d, store_with


def test_numbers_are_normalised():
    assert quality.numbers_in("A $1,000 rise moves rates 10 basis points, 0.40 and 2013-2025; G10, Q1") == \
        ["1000", "10", "0.4", "2013", "2025"]


def test_unverified_numbers():
    src = "Using ten currencies over 2013-2025, a $1 billion shock moves the rate by 0.4 percent."
    ok = {"summary": "Ten currencies, 2013-2025.", "key_takeaways": ["$1 billion moves it 0.4 percent", "10 currencies"]}
    assert quality.unverified_numbers(ok, src) == []
    bad = {"summary": "", "key_takeaways": ["moves it 0.7 percent", "in 2019"]}
    assert quality.unverified_numbers(bad, src) == ["0.7", "2019"]


class Writer(FakeClient):
    """Write-ups quoting a number absent from the abstract, then a clean rewrite."""
    model = "strong"

    def __init__(self, fix=True):
        super().__init__()
        self.fix, self.calls = fix, 0

    def generate_json(self, prompt, system=""):
        out = super().generate_json(prompt, system)
        if isinstance(out, dict) and "key_takeaways" in out:
            self.calls += 1
            bad = "IMPORTANT: a previous answer" not in prompt or not self.fix
            out["key_takeaways"] = ["Rates rise by 7.5 percent." if bad else "Rates rise."] + out["key_takeaways"][1:]
        return out


def test_write_up_is_rewritten_when_a_number_is_not_in_the_source(tmp_path):
    s = store_with(tmp_path, [("a", "Oct [DM] score=8", "2026-10-05")])
    w = Writer()
    analysis.run_analysis(s, FakeClient(), CFG, now=d("2026-11-05"), analysis_client=w)
    a = s.get_analysis(s.all_papers()[0]["uid"])
    assert w.calls == 2 and a["numbers"]["unverified"] == [] and "7.5" not in " ".join(a["key_takeaways"])

    stubborn = Writer(fix=False)
    (tmp_path / "b").mkdir()
    s3 = store_with(tmp_path / "b", [("a", "Oct [DM] score=8", "2026-10-05")])
    analysis.run_analysis(s3, FakeClient(), CFG, now=d("2026-11-05"), analysis_client=stubborn)
    assert s3.get_analysis(s3.all_papers()[0]["uid"])["numbers"]["unverified"] == ["7.5"]


def test_regression_check_against_benchmark(tmp_path):
    rows = [{"id": f"u{i}", "source": "x", "title": f"Paper {i} [{lab}] score=5", "abstract": "We study. " * 30,
             "label_a": lab, "label_b": lab} for i, lab in enumerate(["EM", "DM", "Transversal"] * 4)]
    rows[0]["label_b"] = "DM"                                   # disputed: no reference label
    path = tmp_path / "bench.csv"
    quality.write_benchmark(rows, path)
    res = quality.regression_check(FakeClient(), CFG, path)
    assert res["n"] == 11 and res["agreement"] == 1.0 and res["ok"]

    class Drifted(FakeClient):
        def generate_json(self, prompt, system=""):
            out = super().generate_json(prompt, system)
            for item in out:
                if item["market_focus"] == "transversal":
                    item["market_focus"] = "dm"
            return out
    res = quality.regression_check(Drifted(), CFG, path)
    assert not res["ok"] and len(res["changed"]) == 4 and "Below threshold" in quality.format_regression(res)


def test_monthly_report(tmp_path):
    cfg = {**CFG, "llm": {"analysis_models": ["strong", "lite"]}}
    s = store_with(tmp_path, [("a", "Sep [DM] score=6", "2026-09-10"), ("b", "Oct [DM] score=8", "2026-10-05"),
                              ("c", "Oct EM [EM] score=7", "2026-10-06")])
    publish_issue(s, analysis.run_analysis(s, FakeClient(), cfg, now=d("2026-11-05"), analysis_client=Writer()))
    rep = quality.monthly_report(s, cfg, "2026-10", site_url="https://x.io/rr/")
    assert rep["title"] == "LLM quality check: October 2026 issue" and rep["ok"]
    assert "agree on 2/2" in rep["body"] and "https://x.io/rr/issues/2026-10.html" in rep["body"]

    lite = Writer()
    lite.model = "lite"
    publish_issue(s, analysis.run_analysis(s, FakeClient(), cfg, now=d("2026-11-05"), analysis_client=lite,
                                           rewrite=True))
    rep = quality.monthly_report(s, cfg, "2026-10")
    assert not rep["ok"] and "fallback model" in rep["body"] and "1 check needs attention" in rep["body"]
