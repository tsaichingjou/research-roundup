import json

import pytest

from src.fetchers import openalex
from src.fetchers.http import FetchError

ABS = "We study the transmission of monetary policy to bank lending in the euro area. " * 3


def _inv(text):
    inv = {}
    for i, w in enumerate(text.split()):
        inv.setdefault(w, []).append(i)
    return inv


def work(title, doi):
    return {"id": "https://openalex.org/W1", "doi": f"https://doi.org/{doi}", "display_name": title,
            "publication_date": "2026-10-20", "abstract_inverted_index": _inv(ABS),
            "authorships": [{"author": {"display_name": "Ana Lopez"}}], "primary_location": {}}


INSTITUTIONS = {
    "European Central Bank": [{"id": "https://openalex.org/I1", "display_name": "European Central Bank"}],
    "Bank of Canada": [{"id": "https://openalex.org/I9", "display_name": "Bank of Ghana"}],     # wrong bank
    "Banca d'Italia": [],
    "Bank of Italy": [{"id": "https://openalex.org/I3", "display_name": "Bank of Italy"}],
}


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(openalex, "CACHE", tmp_path / "ids.json")
    calls = []

    def fake(url, settings, params=None):
        calls.append((url, params))
        if url.endswith("/institutions"):
            return json.dumps({"results": INSTITUTIONS.get(params["search"], [])}).encode()
        f = params["filter"]
        if "institutions.id:I1" in f:
            return json.dumps({"results": [work("ECB paper", "10.2866/1")]}).encode()
        if "institutions.id:I3" in f:
            raise FetchError("HTTP 500")
        if "issn:0007-2303" in f:
            return json.dumps({"results": [work("BPEA paper", "10.1353/eca.1")]}).encode()
        return json.dumps({"results": []}).encode()

    monkeypatch.setattr(openalex, "download", fake)
    return calls


INSTS = [
    {"id": "ecb", "name": "European Central Bank", "group": "G10 central banks and ECB"},
    {"id": "boc", "name": "Bank of Canada", "group": "G10 central banks and ECB"},
    {"id": "bdi", "name": "Banca d'Italia", "group": "G10 central banks and ECB", "aliases": ["Bank of Italy"]},
    {"id": "rba", "name": "Reserve Bank of Australia", "group": "Other central banks"},
    {"id": "_fed", "name": "Fed", "skip": True},
]


def test_institution_mode_attributes_and_isolates_failures(api):
    src = {"id": "central_banks", "name": "CB", "institutions": True, "_institutions": INSTS,
           "include_groups": ["G10 central banks and ECB"]}
    papers = openalex.fetch(src, {"contact_email": "a@b.c"})
    assert [(p.source_id, p.source_name, p.title) for p in papers] == [("ecb", "European Central Bank", "ECB paper")]
    p = papers[0]
    assert p.url == "https://doi.org/10.2866/1" and p.abstract.startswith("We study") and p.authors == ["Ana Lopez"]
    d = src["_diagnostics"]
    assert d["European Central Bank"].startswith("1 works")
    assert "not found" in d["Bank of Canada"]                  # "Bank of Ghana" rejected
    assert d["Banca d'Italia"].startswith("error")              # resolved via alias, then query failed
    assert "Reserve Bank of Australia" not in d                  # group not included
    works_calls = [c for c in api if c[0].endswith("/works")]
    assert all("has_abstract:true" in c[1]["filter"] and c[1]["mailto"] == "a@b.c" for c in works_calls)


def test_resolution_is_cached(api):
    src = {"id": "cb", "name": "CB", "institutions": True, "_institutions": INSTS[:1]}
    openalex.fetch(src, {})
    n = sum(c[0].endswith("/institutions") for c in api)
    openalex.fetch(src, {})
    assert sum(c[0].endswith("/institutions") for c in api) == n


def test_issn_mode_for_bpea(api):
    papers = openalex.fetch({"id": "bpea", "name": "BPEA", "issn": "0007-2303", "lookback_days": 365}, {})
    assert [p.title for p in papers] == ["BPEA paper"] and papers[0].source_id == "bpea"


def test_all_institutions_failing_raises(api, monkeypatch):
    monkeypatch.setattr(openalex, "download", lambda *a, **k: (_ for _ in ()).throw(FetchError("down")))
    with pytest.raises(FetchError):
        openalex.fetch({"id": "cb", "name": "CB", "institutions": True, "_institutions": INSTS[:2]}, {})
