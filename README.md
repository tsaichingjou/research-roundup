# The Research Roundup

An automated Python agent that monitors new economics research from leading
journals, central banks and research institutions, and produces a monthly
HTML newsletter of 5–8 curated papers with plain-language summaries and an
EM / DM / Transversal market classification.

> **Status: step 2 of 5.** Ingestion works for NBER, BIS, IMF and the top-5
> journals (AER, Econometrica, JPE, QJE, REStud); the LLM analysis module
> (screening, selection, summaries, EM/DM/Transversal classification) is in place.
> The HTML newsletter, dashboard and scheduling are the next milestones.

## Architecture

```
sources.yaml ──► fetchers ──► SQLite ──► pre-filter ──► LLM screening ──► selection ──► LLM analysis ──► HTML newsletter
  (config)     (rss, crossref)  (dedupe)  (no abstract)   (batched score)   (5-8 papers)  (summary, EM/DM)     (todo)
```

| Path | Role |
|---|---|
| `config/sources.yaml` | Every monitored source, its type and URL. Add or disable sources without touching code. |
| `src/models.py` | `Paper` data model, stable IDs and title normalisation. |
| `src/fetchers/` | One module per source *type*: `rss.py` for any RSS/RDF/Atom feed, `crossref.py` for journals and series with an ISSN (Crossref REST API), `http.py` for shared download/retry logic. |
| `src/storage.py` | SQLite storage with de-duplication by URL **and** by normalised title, plus a `fetch_log` table recording every run. |
| `src/pipeline.py` | Runs all enabled sources; a failing source is logged and skipped, never stops the run. |
| `config/analysis.yaml` | LLM models, time window, pre-filter rules, screening batch size and selection rules. |
| `src/llm.py` | Gemini REST client: JSON mode, throttling, back-off on rate limits, model fallback. |
| `src/prompts.py` | Screening and analysis prompts, including the EM / DM / Transversal definitions. |
| `src/analysis.py` | Pre-filter → batched screening → selection → deep analysis, all cached in SQLite. |
| `main.py` | Command-line entry point. |
| `tests/` | Unit tests using saved feed fixtures (no network needed). |

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then paste your Gemini API key into .env
```

Get a free Gemini API key at <https://aistudio.google.com/apikey>. The `.env`
file is git-ignored, so the key never reaches the repository.

## Usage

```bash
python main.py fetch                 # fetch all enabled sources
python main.py fetch --only nber     # fetch one source
python main.py show --limit 10       # list the latest stored papers
python main.py models                # list Gemini models your key can use
python main.py analyse               # screen, select and analyse this month's papers
python -m pytest -q                  # run tests (no network or API key needed)
```

## Reliability design

- **Timeouts and retries** with exponential back-off on every request.
- **Fault isolation**: each source runs in its own try/except; errors are written to `fetch_log`.
- **Idempotent**: running `fetch` repeatedly never creates duplicates.
- **Stable channels over scraping**: where a publisher has no usable feed (IMF's RSS returns 404; journal sites block bots), papers are read from Crossref by ISSN, which returns structured JSON.
- **No pointless retries**: 4xx errors (wrong URL, blocked) fail immediately; only network/5xx errors are retried.
- **Cross-source de-duplication** by normalised title, so a paper circulated by two institutions appears once.

## Analysis design

**Two LLM stages.** A month brings 150–300 new papers. Every paper with an
abstract is scored in batches of 12 (relevance 0–10, market focus, topic); only
the 5–8 selected papers get the full write-up. This keeps a monthly run to
roughly 20–30 LLM calls, inside the Gemini free tier.

**Pre-filter.** Papers without a usable abstract (common for journal articles
behind paywalls, whose Crossref records often omit it) and non-research items
(comments, replies, errata) are excluded before any LLM call. Every exclusion
is recorded with its reason, and `analyse` prints coverage per source, which
documents where fallbacks were needed.

**Selection.** Highest scores first, at most 2 papers per source so one prolific
institution cannot fill the issue, with a minimum score of 6/10 (relaxed only if
fewer than 5 papers qualify). If no EM paper made the cut but a qualifying one
exists, it replaces the lowest-ranked pick, so EM research is not crowded out by
the larger volume of DM research.

**Classification.** Each selected paper is labelled **EM**, **DM** or
**Transversal**, judged by where its evidence and conclusions apply (definitions
in `src/prompts.py`), with a one- to two-sentence rationale.

**Reliability.** JSON-mode output is validated field by field; invalid answers
are retried once, and papers the model skips are retried on the next run rather
than guessed. All results are cached, so re-running costs no extra calls.
The model never sees anything but the title, authors and abstract, and is
instructed not to state findings that are not in them.

## Roadmap

1. ~~Ingestion pipeline (NBER, BIS, IMF, top-5 journals)~~
2. ~~LLM analysis module — relevance score, summary, key takeaways, EM/DM/Transversal with rationale~~
3. Responsive, email-safe HTML newsletter (Jinja2) and interactive dashboard on GitHub Pages
4. Remaining sources: G10 and European central banks, 12 regional Fed banks, BPEA
5. Weekly/monthly scheduling (GitHub Actions) and sample output
