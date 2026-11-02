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
    monkeypatch.setattr(openalex.time, "sleep", lambda s: None)
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
        if "locations.source.issn:0007-2303|1533-4465" in f:
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
    papers = openalex.fetch({"id": "bpea", "name": "BPEA", "issn": ["0007-2303", "1533-4465"], "lookback_days": 365}, {})
    assert [p.title for p in papers] == ["BPEA paper"] and papers[0].source_id == "bpea"


def test_all_institutions_failing_raises(api, monkeypatch):
    monkeypatch.setattr(openalex, "download", lambda *a, **k: (_ for _ in ()).throw(FetchError("down")))
    with pytest.raises(FetchError):
        openalex.fetch({"id": "cb", "name": "CB", "institutions": True, "_institutions": INSTS[:2]}, {})


def test_name_key_folds_accents_and_never_matches_empty():
    assert openalex.name_key("Österreichische Nationalbank") == "osterreichische nationalbank"
    assert openalex.name_key("Banco de España") == openalex.name_key("Banco de Espana")
    assert openalex.name_key("Τράπεζα της Ελλάδος") == ""


def test_download_waits_on_429_then_succeeds(monkeypatch):
    from src.fetchers import http

    class Resp:
        def __init__(self, code):
            self.status_code, self.headers, self.content = code, {"Retry-After": "7"}, b"ok"

        def raise_for_status(self):
            pass

    seq = iter([Resp(429), Resp(200)])
    waits = []
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: next(seq))
    monkeypatch.setattr(http.time, "sleep", waits.append)
    assert http.download("https://api.openalex.org/works", {"max_retries": 3}) == b"ok"
    assert waits == [7.0]


def test_long_retry_after_means_quota_exceeded(monkeypatch):
    from src.fetchers import http

    class Resp:
        status_code, headers = 429, {"Retry-After": "34734"}

    sleeps = []
    monkeypatch.setattr(http.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(http.time, "sleep", sleeps.append)
    with pytest.raises(http.QuotaExceeded, match="resets in about 9.6 h"):
        http.download("https://api.openalex.org/works", {"max_retries": 3})
    assert sleeps == []                                   # no pointless waiting


def test_quota_exceeded_stops_remaining_banks_but_keeps_results(api, monkeypatch):
    from src.fetchers.http import QuotaExceeded
    inner = openalex.download

    def quota_after_ecb(url, settings, params=None):
        if "institutions.id:I1" not in (params or {}).get("filter", "") and url.endswith("/works"):
            raise QuotaExceeded("api.openalex.org daily budget used up")
        return inner(url, settings, params)

    monkeypatch.setattr(openalex, "download", quota_after_ecb)
    insts = [{**INSTS[0], "openalex_id": "I1"}, {**INSTS[2], "openalex_id": "I3"},
             {"id": "x", "name": "Another Bank", "group": "G", "openalex_id": "I7"}]
    src = {"id": "cb", "name": "CB", "institutions": True, "_institutions": insts}
    papers = openalex.fetch(src, {})
    assert [p.title for p in papers] == ["ECB paper"]
    assert src["_diagnostics"]["Banca d'Italia"].startswith("skipped")
    assert src["_diagnostics"]["Another Bank"].startswith("skipped")
    assert not any(c[0].endswith("/institutions") for c in api)   # pinned IDs: no paid search


def test_enrichment_stops_using_openalex_after_quota(monkeypatch):
    from src import enrich
    from src.fetchers.http import QuotaExceeded
    calls = []

    def fake(url, settings, params=None):
        calls.append(url)
        if "openalex" in url:
            raise QuotaExceeded("budget used up")
        raise FetchError("HTTP 403")

    monkeypatch.setattr(enrich, "download", fake)
    e = enrich.Enricher({})
    for i in range(3):
        e.enrich({"uid": str(i), "title": "T", "url": f"https://doi.org/10.1/{i}"})
    assert sum("openalex" in c for c in calls) == 1 and e.quota_note


def test_journal_falls_back_to_source_name(api, monkeypatch):
    inner = openalex.download

    def fake(url, settings, params=None):
        if url.endswith("/sources"):
            return json.dumps({"results": [{"id": "https://openalex.org/S42",
                                            "display_name": "Brookings Papers on Economic Activity",
                                            "issn": ["0007-2303"]}]}).encode()
        if "issn:" in (params or {}).get("filter", ""):
            return json.dumps({"results": []}).encode()
        if "source.id:S42" in (params or {}).get("filter", ""):
            return json.dumps({"results": [work("Demand-Driven Inflation", "10.1353/eca.2")]}).encode()
        return inner(url, settings, params)

    monkeypatch.setattr(openalex, "download", fake)
    src = {"id": "bpea", "name": "BPEA", "issn": ["0007-2303"],
           "source_name": "Brookings Papers on Economic Activity", "lookback_days": 365}
    papers = openalex.fetch(src, {})
    assert [p.title for p in papers] == ["Demand-Driven Inflation"]
    assert src["_diagnostics"]["by ISSN"].startswith("0 works")
    assert src["_diagnostics"]["by name"].startswith("1 works (S42")
