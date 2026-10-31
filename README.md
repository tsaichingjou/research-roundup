# The Research Roundup

An automated Python agent that monitors new economics research from leading
journals, central banks and research institutions, and produces a monthly
HTML newsletter of 5–8 curated papers with plain-language summaries and an
EM / DM / Transversal market classification.

> **Status: step 4 of 5.** All required source groups are configured, with LLM
> analysis, the email-ready newsletter and the interactive explorer in place.
> Remaining: automated weekly screening and monthly publishing to GitHub Pages.

## Architecture

```
sources.yaml ─► fetchers ─► SQLite ─► pre-filter ─► LLM screening ─► selection ─► LLM analysis ─► issue ─► site (docs/)
  (config)   (rss, crossref) (dedupe) (no abstract)  (batched score)  (5-8 papers) (summary, EM/DM)  (frozen)  newsletter + explorer
```

| Path | Role |
|---|---|
| `config/sources.yaml` | Every monitored source, its type and URL. Add or disable sources without touching code. |
| `config/institutions.yaml` | Central banks reached through the BIS Research Hub feed: domain → institution and group. |
| `src/models.py` | `Paper` data model, stable IDs and title normalisation. |
| `src/fetchers/` | One module per source *type*: `rss.py` for any RSS/RDF/Atom feed, `crossref.py` for journals and series with an ISSN (Crossref REST API), `http.py` for shared download/retry logic. |
| `src/storage.py` | SQLite storage with de-duplication by URL **and** by normalised title, plus a `fetch_log` table recording every run. |
| `src/pipeline.py` | Runs all enabled sources; a failing source is logged and skipped, never stops the run. |
| `config/analysis.yaml` | LLM models, time window, pre-filter rules, screening batch size and selection rules. |
| `src/llm.py` | Gemini REST client: JSON mode, throttling, back-off on rate limits, model fallback. |
| `src/prompts.py` | Screening and analysis prompts, including the EM / DM / Transversal definitions. |
| `src/analysis.py` | Pre-filter → batched screening → selection → deep analysis, all cached in SQLite. |
| `src/issues.py` | Freezes the selection into a monthly issue with an editor's note; past issues never change. |
| `src/site.py`, `templates/` | Builds the static site: one email-ready newsletter per issue and the interactive explorer. |
| `config/site.yaml` | Site title and public URL (used for links inside the emailed newsletter). |
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
python main.py check-sources         # test every feed without storing: items, abstracts, newest date
python main.py show --limit 10       # list the latest stored papers
python main.py models                # list Gemini models your key can use
python main.py check                 # test each configured model and its response time
python main.py analyse               # screen, select and analyse this month's papers
python main.py analyse --rewrite     # regenerate write-ups (after a prompt change, or with a stronger model)
python main.py publish               # analyse, freeze this month's issue, build the site in docs/
python main.py build                 # rebuild docs/ from the database (no LLM calls)
python main.py export-labels         # blind, stratified labelling sample -> evaluation/labels.csv
python main.py evaluate              # compare LLM classification with the human labels
python main.py evaluate --rerun      # same, re-classifying the sample with the current prompt
python -m pytest -q                  # run tests (no network or API key needed)
```

## Source coverage

| Required in the brief | How it is covered | Channel |
|---|---|---|
| AER, Econometrica, JPE, QJE, REStud | One source per journal | Crossref API by ISSN (90-day look-back, as journals register DOIs in batches) |
| NBER | All new working papers | NBER RSS |
| BPEA | Spring and autumn editions | Crossref API by ISSN (1-year look-back; no feed exists) |
| IMF | IMF Working Papers | Crossref API by ISSN (the IMF RSS endpoint returns 404) |
| BIS | BIS Working Papers | BIS RSS |
| 12 Federal Reserve regional banks + Board | One source per bank | Fed in Print RSS (the Federal Reserve System's own catalogue) |
| G10 central banks | ECB, Bank of Japan, Bank of England, Bank of Canada, Bundesbank, Banque de France, Banca d'Italia, De Nederlandsche Bank, National Bank of Belgium, Riksbank, SNB (Fed via Fed in Print) | BIS Central Bank Research Hub RSS |
| European national central banks | 25 banks, from Banco de España to Norges Bank and the NBP | BIS Central Bank Research Hub RSS |

The BIS Central Bank Research Hub republishes new research from more than 50
central banks in a single feed, each item linking to the paper on the issuing
bank's website. The agent attributes every item to that bank from the link's
domain (`config/institutions.yaml`), so the newsletter credits, say, the Bank of
Japan rather than the BIS; Fed and BIS items are skipped there because they are
fetched directly. The Hub also carries emerging-market central banks (Brazil,
Mexico, Chile, Korea and others), which strengthens EM coverage. One feed for
dozens of banks is far more reliable than scraping dozens of differently built
websites; the trade-off is dependence on the Hub's update lag.

`python main.py check-sources` fetches every feed without storing anything and
reports items, how many have usable abstracts, and the newest date — the first
thing to run when a source looks quiet.

**Known fallbacks.** JPE and some journal records carry no abstract in Crossref,
so those papers are listed but not assessed (as agreed for paywalled sources);
Fed in Print feeds also include speeches and articles, which are excluded when
they lack an abstract or are screened out as low relevance.

## Reliability design

- **Timeouts and retries** with exponential back-off on every request.
- **Fault isolation**: each source runs in its own try/except; errors are written to `fetch_log`.
- **Idempotent**: running `fetch` repeatedly never creates duplicates.
- **Stable channels over scraping**: where a publisher has no usable feed (IMF's RSS returns 404; journal sites block bots), papers are read from Crossref by ISSN, which returns structured JSON.
- **No pointless retries**: 4xx errors (wrong URL, blocked) fail immediately; only network/5xx errors are retried.
- **Cross-source de-duplication** by normalised title, so a paper circulated by two institutions appears once.

## Analysis design

**Two LLM stages.** A month brings 150–300 new papers. Every paper with an
abstract is scored in batches of 8 (relevance 0–10, market focus, topic); only
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
in `src/prompts.py`), with a one- to two-sentence rationale. The label must rest
on countries or data the abstract actually names: a theory paper that names no
economy is Transversal, rather than being inferred as DM from the type of model
it uses (an error observed in the first test run and corrected in the prompt).

**Models.** A fast "lite" model screens; a stronger model writes. Each list in
`config/analysis.yaml` falls back automatically when a model is unavailable,
overloaded (HTTP 5xx) or too slow — on the Gemini free tier the larger models
are often overloaded at peak times. Each write-up records the model that
produced it.

**Reliability.** JSON-mode output is validated field by field; invalid answers
are retried once, and papers the model skips are retried on the next run rather
than guessed. All results are cached, so re-running costs no extra calls.
The model never sees anything but the title, authors and abstract, and is
instructed not to state findings that are not in them.

## Newsletter and explorer

`python main.py publish` writes the site to `docs/` (served by GitHub Pages):

- **`issues/YYYY-MM.html` — the newsletter.** Built for email as well as the web:
  table layout and inline styles (Outlook, Gmail and Apple Mail ignore most modern
  CSS), web-safe fonts, a 640px column that narrows on phones. Each issue opens
  with an editor's note and a composition strip, one segment per featured paper
  coloured by market focus, then gives every paper a plain-language summary,
  three takeaways, why it matters and the classification rationale. A closing
  "How this issue was made" section states what was screened, which sources
  could not be assessed and why, and which model wrote the summaries.
- **`index.html` — the explorer.** Pick any issue, switch between its featured
  papers and everything screened that month, filter by market focus, source or
  free text, and open a paper to see its summary, takeaways, rationale,
  relevance score, topics, regions, the model that wrote it and the abstract.
  A source-coverage chart and table show, per source, how many papers were
  screened, how they were classified and how many could not be assessed.
  The data is embedded in the page, so it also works opened as a local file;
  the same data is published as `data/site.json`.
- **Design.** The three market-focus colours (EM ochre, DM blue, Transversal
  teal) were checked for colour-blind separation, and every tag also carries its
  text label, so meaning never depends on colour alone. Responsive down to
  phone width, keyboard-accessible, and without external scripts.

Issues are frozen when published: re-running `publish` in the same month
replaces that month's issue, but earlier issues are never changed.

## Evaluating the classification

The EM / DM / Transversal labels are checked against human labels rather than
taken on trust (`src/evaluation.py`):

- **Blind, stratified sample.** `export-labels` draws ~15 papers per LLM class
  (DM papers far outnumber EM ones, so a random sample would leave EM recall
  resting on a handful of papers) and writes only titles and abstracts, so the
  annotator is not anchored by the LLM's answer. Labelling rules:
  `evaluation/LABELLING_GUIDE.md`.
- **Dev / test split.** Errors on `dev` rows may be used to improve the prompt;
  the reported score comes from `test` rows not looked at while tuning.
- **Metrics.** Accuracy with a Wilson 95% interval, confusion matrix,
  per-class precision / recall / F1, macro-F1 (robust to class imbalance) and
  Cohen's κ (agreement beyond chance).
- **Consistency without labels.** The screening model and the writing model
  label each selected paper independently; their agreement rate is reported too.

Results: see `evaluation/report.txt`.

## Roadmap

1. ~~Ingestion pipeline (NBER, BIS, IMF, top-5 journals)~~
2. ~~LLM analysis module — relevance score, summary, key takeaways, EM/DM/Transversal with rationale~~
3. ~~Responsive, email-safe HTML newsletter and interactive explorer~~
4. ~~Remaining sources: G10 and European central banks, 12 regional Fed banks, BPEA~~
5. Weekly fetch / monthly publish with GitHub Actions, deployed to GitHub Pages
