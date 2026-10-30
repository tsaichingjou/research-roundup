# The Research Roundup

An automated Python agent that monitors new economics research from leading
journals, central banks and research institutions, and produces a monthly
HTML newsletter of 5–8 curated papers with plain-language summaries and an
EM / DM / Transversal market classification.

> **Status: step 1 of 5 — data ingestion.** The fetch → store pipeline works
> for NBER, BIS, IMF and the top-5 journals (AER, Econometrica, JPE, QJE, REStud). Screening, LLM analysis, the newsletter and scheduling
> are the next milestones.

## Architecture

```
sources.yaml ──► fetchers ──► SQLite (dedupe) ──► screening ──► LLM analysis ──► HTML newsletter
  (config)       (rss, …)       storage.py          (todo)         (todo)            (todo)
```

| Path | Role |
|---|---|
| `config/sources.yaml` | Every monitored source, its type and URL. Add or disable sources without touching code. |
| `src/models.py` | `Paper` data model, stable IDs and title normalisation. |
| `src/fetchers/` | One module per source *type*: `rss.py` for any RSS/RDF/Atom feed, `crossref.py` for journals and series with an ISSN (Crossref REST API), `http.py` for shared download/retry logic. |
| `src/storage.py` | SQLite storage with de-duplication by URL **and** by normalised title, plus a `fetch_log` table recording every run. |
| `src/pipeline.py` | Runs all enabled sources; a failing source is logged and skipped, never stops the run. |
| `main.py` | Command-line entry point. |
| `tests/` | Unit tests using saved feed fixtures (no network needed). |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

```bash
python main.py fetch                 # fetch all enabled sources
python main.py fetch --only nber     # fetch one source
python main.py show --limit 10       # list the latest stored papers
python -m pytest -q                  # run tests
```

## Reliability design

- **Timeouts and retries** with exponential back-off on every request.
- **Fault isolation**: each source runs in its own try/except; errors are written to `fetch_log`.
- **Idempotent**: running `fetch` repeatedly never creates duplicates.
- **Stable channels over scraping**: where a publisher has no usable feed (IMF's RSS returns 404; journal sites block bots), papers are read from Crossref by ISSN, which returns structured JSON.
- **No pointless retries**: 4xx errors (wrong URL, blocked) fail immediately; only network/5xx errors are retried.
- **Cross-source de-duplication** by normalised title, so a paper circulated by two institutions appears once.

## Roadmap

1. ~~Ingestion pipeline (NBER, BIS, IMF, top-5 journals)~~
2. LLM analysis module — relevance score, summary, key takeaways, EM/DM/Transversal with rationale (JSON output)
3. Responsive, email-safe HTML newsletter (Jinja2)
4. Remaining sources: G10 and European central banks, 12 regional Fed banks, BPEA
5. Weekly/monthly scheduling (GitHub Actions) and sample output
