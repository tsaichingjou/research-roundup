import csv

import pytest

from src import evaluation
from src.models import Paper
from src.storage import Storage
from tests.test_analysis import ABSTRACT, CFG, FakeClient


def test_metrics_match_hand_calculation():
    # human, llm
    pairs = ([("DM", "DM")] * 8 + [("DM", "Transversal")] * 2
             + [("EM", "EM")] * 4 + [("EM", "Transversal")] * 1
             + [("Transversal", "Transversal")] * 4 + [("Transversal", "DM")] * 1)
    m = evaluation.compute_metrics(pairs)
    assert m["n"] == 20
    assert m["accuracy"] == pytest.approx(16 / 20)
    assert m["per_class"]["DM"]["recall"] == pytest.approx(8 / 10)
    assert m["per_class"]["DM"]["precision"] == pytest.approx(8 / 9)
    assert m["per_class"]["EM"]["recall"] == pytest.approx(4 / 5)
    assert m["per_class"]["Transversal"]["precision"] == pytest.approx(4 / 7)
    # kappa: po = 0.8; pe = (10*9 + 5*4 + 5*7) / 400 = 145/400
    assert m["kappa"] == pytest.approx((0.8 - 145 / 400) / (1 - 145 / 400))
    assert m["confusion"]["DM"]["Transversal"] == 2
    lo, hi = m["accuracy_ci"]
    assert 0.58 < lo < 0.60 and 0.91 < hi < 0.93          # Wilson interval for 16/20


def test_perfect_agreement_gives_kappa_one():
    m = evaluation.compute_metrics([("EM", "EM"), ("DM", "DM"), ("Transversal", "Transversal")])
    assert m["accuracy"] == 1 and m["kappa"] == pytest.approx(1.0)


def _store_with_screening(tmp_path):
    store = Storage(tmp_path / "t.db")
    specs = [("nber", f"Paper {i} [{lab}] score=7") for lab in ("EM", "DM", "Transversal") for i in range(6)]
    store.save_papers([Paper(source_id=s, source_name=s, title=t, url=f"https://x.org/{i}",
                             abstract=ABSTRACT) for i, (s, t) in enumerate(specs)])
    from src import analysis
    analysis.run_analysis(store, FakeClient(), {**CFG, "selection": {**CFG["selection"], "max_per_source": 9}})
    return store


def test_export_is_blind_stratified_and_split(tmp_path):
    store = _store_with_screening(tmp_path)
    path = tmp_path / "labels.csv"
    counts = evaluation.export_sample(store, path, per_class=3)
    assert counts == {"EM": 3, "DM": 3, "Transversal": 3}
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 9
    assert set(rows[0]) == set(evaluation.FIELDS)            # no LLM label column
    assert all(r["human_label"] == "" for r in rows)
    assert sum(r["split"] == "test" for r in rows) == 3        # 1 per class


def test_evaluate_reports_errors_and_rerun(tmp_path):
    store = _store_with_screening(tmp_path)
    path = tmp_path / "labels.csv"
    evaluation.export_sample(store, path, per_class=2)
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    for r in rows:   # human agrees with the tag in the title, except one disagreement
        r["human_label"] = r["title"].split("[")[1].split("]")[0]
    rows[0]["human_label"] = "t" if rows[0]["human_label"] != "Transversal" else "DM"
    rows[1]["human_label"] = ""                                 # unlabelled rows are skipped
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=evaluation.FIELDS)
        w.writeheader()
        w.writerows(rows)

    result = evaluation.evaluate(store, path)
    assert result["n_labelled"] == 5
    assert len(result["errors"]) == 1
    assert result["splits"]["all"]["accuracy"] == pytest.approx(4 / 5)
    assert "Confusion matrix" in evaluation.format_report(result)

    client = FakeClient()
    rerun = evaluation.evaluate(store, path, client=client, cfg=CFG)
    assert "re-classified" in rerun["source"] and len(client.prompts) >= 1


def test_evaluate_without_labels_raises(tmp_path):
    store = _store_with_screening(tmp_path)
    path = tmp_path / "labels.csv"
    evaluation.export_sample(store, path, per_class=2)
    with pytest.raises(ValueError):
        evaluation.evaluate(store, path)


def test_model_agreement_finds_disagreements(tmp_path):
    store = _store_with_screening(tmp_path)           # 6 papers per class, labelled by model A

    class ModelB(FakeClient):
        model = "model-b"

        def generate_json(self, prompt, system=""):
            out = super().generate_json(prompt, system)
            for item in out:                          # B calls every EM paper Transversal
                if item["market_focus"] == "em":
                    item["market_focus"] = "transversal"
            return out
    res = evaluation.model_agreement(store, ModelB(), CFG, per_class=4)
    assert res["n"] == 12 and res["model_b"] == "model-b"
    assert res["metrics"]["accuracy"] == pytest.approx(8 / 12)
    assert res["agree_by_class_a"] == {"EM": 0.0, "DM": 1.0, "Transversal": 1.0}
    n = evaluation.write_disagreements(res, tmp_path / "d.csv")
    assert n == 4
    report = evaluation.format_agreement(res)
    assert "not accuracy" in report and "Disagreements (4)" in report
