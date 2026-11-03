# The Research Roundup

An automated Python agent that monitors new economics research from leading
journals, central banks and research institutions, and produces a monthly
HTML newsletter of 5–8 curated papers with plain-language summaries and an
EM / DM / Transversal market classification.

**Live newsletter and explorer:** <https://tsaichingjou.github.io/research-roundup/>
(each issue is under `issues/YYYY-MM.html`; the explorer links to it).

The agent runs itself on GitHub Actions: every Monday it fetches and scores the
week's new publications, and on the 1st of each month it selects 5–8 papers,
writes them up and publishes the issue to GitHub Pages.

## Architecture

```
sources.yaml ─► fetchers ─► SQLite ─► pre-filter ─► LLM screening ─► selection ─► LLM analysis ─► issue ─► site (docs/)
  (config)   (rss, crossref) (dedupe) (no abstract)  (batched score)  (5-8 papers) (summary, EM/DM)  (frozen)  newsletter + explorer
```

| Path | Role |
|---|---|
| `config/sources.yaml` | Every monitored source, its type and URL. Add or disable sources without touching code. |
| `config/institutions.yaml` | The central banks covered: name, group, OpenAlex aliases and web domains. |
| `src/models.py` | `Paper` data model, stable IDs and title normalisation. |
| `src/fetchers/` | One module per source *type*: `rss.py` for any RSS/RDF/Atom feed, `crossref.py` for journals with an ISSN (Crossref API), `openalex.py` for works by institution or ISSN (OpenAlex API), `http.py` for shared download/retry logic. |
| `src/storage.py` | SQLite storage with de-duplication by URL **and** by normalised title, plus a `fetch_log` table recording every run. |
| `src/pipeline.py` | Runs all enabled sources; a failing source is logged and skipped, never stops the run. |
| `config/analysis.yaml` | LLM models, time window, pre-filter rules, screening batch size and selection rules. |
| `src/llm.py` | Gemini REST client: JSON mode, throttling, back-off on rate limits, model fallback. |
| `src/prompts.py` | Screening and analysis prompts, including the EM / DM / Transversal definitions. |
| `src/enrich.py` | Recovers missing abstracts from OpenAlex, publisher pages or the opening of the paper's text before papers are written off. |
| `src/fulltext.py` | Finds and reads a paper's open full text (PDF or web page) for the write-ups. |
| `src/analysis.py` | Pre-filter → batched screening → selection → deep analysis, all cached in SQLite. |
| `src/issues.py` | Freezes the selection into a monthly issue with an editor's note; past issues never change. |
| `src/site.py`, `templates/` | Builds the static site: one email-ready newsletter per issue and the interactive explorer. |
| `config/site.yaml` | Site title, public URL (used for links inside the emailed newsletter), audience line. |
| `.github/workflows/` | Daily screening and monthly publishing on GitHub Actions, deployment to GitHub Pages, tests on every push. |
| `main.py` | Command-line entry point. |
| `tests/` | Unit tests using saved feed fixtures (no network needed). |

## Running it yourself

```bash
git clone https://github.com/tsaichingjou/research-roundup.git
cd research-roundup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env               # paste a free Gemini API key into .env
python main.py fetch               # ~550 papers from 23 sources, a few minutes
python main.py publish             # screen, select, write up, build the site
open site/index.html               # the explorer; the newsletter is in site/issues/
```

A full first run makes roughly 70–90 Gemini calls (scoring in batches of 8,
then 5–8 write-ups), within the free tier. `python -m pytest -q` runs the test
suite without network access or API keys.

## Deployment and scheduling

`.github/workflows/pipeline.yml` runs the agent on GitHub Actions:

| When | Job | Steps |
|---|---|---|
| Every day, 05:23 UTC | Daily screening | `fetch` (with abstract recovery) → `screen` → `build` (refreshes the month in progress and its provisional picks) |
| 1st of the month, 06:17 UTC | Monthly issue | `fetch` → `publish` (select from the month that just ended, write up, freeze the issue, build) |
| On demand | Daily, monthly or republish | "Run workflow" button in the Actions tab; *republish* retries every missing abstract and rewrites last month's issue (`enrich --retry-all` → `publish --rewrite`), e.g. after a prompt change |

Each run commits its database (`data/published.db`) and the built site
(`docs/`) back to the repository, so the next run continues where the last one
stopped and the latest output can be read straight from GitHub, and then deploys
`docs/` to GitHub Pages. Scores accumulate during the month, so the monthly run
only selects and writes up. Runs never overlap (a concurrency group), and the
scheduled jobs use their own database and site folder so they cannot clash with
a local copy. `tests.yml` runs the test suite on every push.

Setting up a fork: add the repository secrets `GEMINI_API_KEY` (required) and
`OPENALEX_API_KEY` (optional), set *Settings → Pages → Source* to
*GitHub Actions*, then start the workflow once from the Actions tab.

**Periods.** Each issue covers one calendar month: the October issue holds
research published 1-31 October and is produced on 1 November; re-running it
later (`publish --rewrite`, or `--month 2026-10`) keeps the same window. A paper
dated in October that only reaches a source after the October issue went out
joins the November issue as a *late arrival* (counted in the newsletter's notes),
so nothing is lost to indexing delays. During the month the explorer shows the
month in progress with a provisional selection: the papers that would be chosen
if the issue were made that day.

**Daily vs monthly.** The brief asks the agent to monitor weekly publications
and publish a monthly newsletter. Screening runs daily (a few cheap batch calls)
so the dashboard is never more than a day behind, and its "Published this week"
view shows the latest week; the newsletter itself is monthly. The newsletter HTML is email-ready, so sending it would only need an
SMTP step in the monthly job; that was left out because the brief asks for an
email-ready or web-viewable newsletter, not for a distribution list.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then paste your Gemini API key into .env
```

Get a free Gemini API key at <https://aistudio.google.com/apikey>, and
optionally a free OpenAlex key at <https://openalex.org/settings/api>
(`OPENALEX_API_KEY`; it raises OpenAlex's daily budget tenfold). The `.env`
file is git-ignored, so keys never reach the repository.

## Usage

```bash
python main.py fetch                 # fetch all enabled sources
python main.py fetch --only nber     # fetch one source
python main.py check-sources         # test every feed without storing: items, abstracts, newest date
python main.py enrich                # recover missing abstracts (also runs automatically after fetch)
python main.py show --limit 10       # list the latest stored papers
python main.py models                # list Gemini models your key can use
python main.py check                 # test each configured model and its response time
python main.py screen                # daily job: score new papers, no write-ups
python main.py analyse               # screen, select and analyse this month's papers
python main.py analyse --rewrite     # regenerate write-ups (after a prompt change, or with a stronger model)
python main.py publish               # analyse, freeze last month's issue, build the site in site/
python main.py publish --month 2026-10   # build (or rebuild) a specific month's issue
python main.py build                 # rebuild site/ from the database (no LLM calls)
python main.py export-labels         # blind, stratified labelling sample -> evaluation/labels.csv
python main.py evaluate              # compare LLM classification with the human labels
python main.py evaluate --rerun      # same, re-classifying the sample with the current prompt
python -m pytest -q                  # run tests (no network or API key needed)
```

## Source coverage

| Required in the brief | How it is covered | Channel |
|---|---|---|
| AER, Econometrica, QJE, REStud | One source per journal | Crossref API by ISSN (90-day look-back, as journals register DOIs in batches) |
| JPE | **Partial**: only papers whose abstract can be recovered (about 40% in testing); see below | Crossref API by ISSN + OpenAlex for abstracts |
| NBER | All new working papers | NBER RSS |
| BPEA | Spring and autumn editions | OpenAlex API by ISSN (1-year look-back; no feed exists, and Crossref returned nothing) |
| IMF | IMF Working Papers | Crossref API by ISSN (the IMF RSS endpoint returns 404) |
| BIS | BIS Working Papers | BIS RSS |
| 12 Federal Reserve regional banks + Board | One source per bank | Fed in Print RSS (the Federal Reserve System's own catalogue) |
| G10 central banks | ECB, Bank of Japan, Bank of England, Bank of Canada, Bundesbank, Banque de France, Banca d'Italia, De Nederlandsche Bank, National Bank of Belgium, Riksbank, SNB (Fed via Fed in Print) | OpenAlex API, one query per bank; BIS Central Bank Research Hub RSS as a supplement |
| European national central banks | 25 banks, from Banco de España to Norges Bank and the NBP | Same |

**Why JPE is only partly covered.** The brief allows a source to be dropped
for a cycle when abstracts are unavailable, provided this is documented. JPE
was kept with partial coverage rather than dropped, for these reasons:

1. JPE's Crossref records carry no abstracts (0 of 25 recent records, against
   full coverage for AER, Econometrica, QJE and REStud).
2. The abstracts are public on the journal's site, but its pages block
   automated requests, and the agent does not try to get around that.
3. OpenAlex holds abstracts for part of JPE's output: 15 of 39 recent papers
   in the first run.

So JPE papers with an OpenAlex abstract are screened like any other paper,
and the rest get a market-focus label from their title alone (one cheap batch
call to the lite model). That keeps them in the coverage chart and the explorer,
marked "labelled from title", but they are not scored and cannot be selected:
a title is too little to rank on. Each newsletter's "How this issue was made"
note says how many there were.
Missing abstracts are looked up again every week, as OpenAlex keeps adding
records. The trade-off is that a relevant JPE paper can be missed in the month
it appears; to drop JPE instead, set `enabled: false` in `config/sources.yaml`.

**Central banks.** Each central bank publishes on its own differently built
website. The first design used the BIS Central Bank Research Hub, which
republishes research from 50+ central banks in one feed; a live check on
31 Oct 2026 showed it carried only three items, the newest from 1 August. The
agent therefore queries **OpenAlex**, a free index of scholarly works, once per
central bank in `config/institutions.yaml` for recent works by that bank's
researchers, keeping only records with an abstract. Bank names are matched to
OpenAlex institution IDs exactly (with aliases such as "Bank of Italy" for Banca
d'Italia), cached, and can be pinned by ID; `check-sources` lists the match and
the number of works for every bank. Each paper is credited to its bank, so the
newsletter cites the Bank of Japan, not an aggregator. Requests are spaced out
and short rate-limit responses (HTTP 429) are waited out.

**OpenAlex budget.** OpenAlex bills usage against a daily budget: $0.10 without
a key, $1 with a free key (set `OPENALEX_API_KEY` in `.env`). Name searches cost
ten times more than filtered lists, and single-record lookups by DOI are free.
The agent is built around this: central-bank IDs are pinned in
`config/institutions.yaml` (no searches needed), abstract recovery prefers
free DOI lookups and caps paid title searches per run, and when the budget is
exhausted (a 429 asking to wait hours) the run stops calling OpenAlex at once,
keeps everything fetched so far and reports when the budget resets, instead of
retrying for hours. During development the anonymous budget ran out in one
afternoon of repeated test runs, which is what prompted these safeguards. The Research Hub stays on as a supplement, with items attributed to
their bank from the link's domain and Fed or BIS items skipped as duplicates.

Coverage is limited to the groups the brief names (G10 and European central
banks). `config/institutions.yaml` also lists emerging-market central banks
(Brazil, Mexico, Korea, South Africa and others); adding `Other central banks`
to `include_groups` in `sources.yaml` turns them on for extra EM coverage.

**Known gaps from the first live run.** OpenAlex returned no recent works for
a few banks (for example the Bank of Japan), which usually means their working
papers are indexed late or without author affiliations. `check-sources` shows
these per bank, and a bank can be pinned to a specific OpenAlex ID if the name
match is wrong.

`python main.py check-sources` fetches every feed without storing anything and
reports items, how many have usable abstracts, and the newest date — the first
thing to run when a source looks quiet.

**Missing abstracts.** Some channels deliver titles without abstracts (Crossref
records for JPE, Fed in Print speeches and blog posts, some central-bank
working papers). After every fetch the agent tries three open sources before
giving up on a paper (`src/enrich.py`): **OpenAlex**, a free scholarly index, by
DOI or near-exact title; the abstract metadata tags (`citation_abstract`,
`dc.description`, `og:description`) on the paper's own page; and finally the
**opening of the paper's own text** (`src/fulltext.py`) — the section after an
"Abstract" heading in the PDF, or the first paragraphs of a speech or article,
capped at the same 1,500 characters used for screening, so this costs no more
Gemini tokens than an abstract. Only openly published pages are read: a page
that answers with an error, a login or a bot check is skipped, so no paywall is
bypassed. The origin of each recovered text is stored and shown in the
explorer. Papers still without one (in practice mostly JPE) are labelled by
market focus from their title only, not scored, and counted in every issue. Replication datasets that central banks deposit in OpenAlex (Zenodo,
Dataverse, Mendeley Data) are filtered out as not being papers.

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

**Full-text write-ups.** Screening uses abstracts (cheap, and enough to rank
papers), but the 5–8 selected papers are written up from their **open full
text** when it exists: the paper's own PDF, the PDF its landing page declares
(`citation_pdf_url`), the publishing bank's page for Fed in Print records, or an
open-access copy listed by OpenAlex. The text is read with `pypdf`, the
reference list is dropped and it is capped at 60,000 characters (about 15k
tokens: introduction, results and conclusions), so the key takeaways can quote
actual estimates, samples and periods instead of paraphrasing the abstract. The
model is told to use only numbers that appear in the text. Where no open text
exists (paywalled journals) or it cannot be read, the entry falls back to the
abstract. Each entry states which it was written from, in the newsletter and in
the explorer. This adds about one screening run's worth of tokens per month
(8 requests); `publish` prints the Gemini requests and tokens used per model.

**Reliability.** JSON-mode output is validated field by field; invalid answers
are retried once, and papers the model skips are retried on the next run rather
than guessed. All results are cached, so re-running costs no extra calls.
The model sees only the title, authors, abstract and (for write-ups) the open
full text, and is instructed not to state findings that are not in them.

## Newsletter and explorer

`python main.py publish` builds the site (into `site/` locally; the scheduled
run builds `docs/` and deploys it to GitHub Pages):

- **`issues/YYYY-MM.html` — the newsletter.** Built for email as well as the web:
  table layout and inline styles (Outlook, Gmail and Apple Mail ignore most modern
  CSS), web-safe fonts, a 640px column that narrows on phones. Each issue opens
  with an editor's note and a composition strip, one segment per featured paper
  coloured by market focus, then gives every paper a plain-language summary,
  three takeaways, why it matters and the classification rationale. A closing
  "How this issue was made" section states what was screened, which sources
  could not be assessed and why, and which model wrote the summaries.
- **`index.html` — the explorer**, in three views that all follow one period
  selector (a published issue, the month in progress, or all papers):
  - *Papers*: the issue's chosen papers (or, for the month in progress, the
    provisional picks), every paper of the period, or those published this
    week; filter by market focus, source or free text, and open a paper to see
    its summary, takeaways, rationale, relevance score, topics, regions, the
    model that wrote it and the abstract.
  - *Source coverage*: a chart and table of how many publications each source
    contributed in the period, by market focus.
  - *How it works*: the relevance scale, the market-focus definitions, the
    selection rules, the monthly timeline, how missing abstracts are handled and
    a glossary of the dashboard's terms, for readers who receive the link
    without any context.
  The data is embedded in the page, so it also works opened as a local file;
  the same data is published as `data/site.json`.
- **Design.** The three market-focus colours (EM ochre, DM blue, Transversal
  teal) were checked for colour-blind separation, and every tag also carries its
  text label, so meaning never depends on colour alone. Responsive down to
  phone width, keyboard-accessible, and without external scripts.

**"In the newsletter"** marks the 5–8 papers chosen for an issue: the highest
relevance scores, at least 6/10, no more than two per source, with an
emerging-markets paper swapped in when one qualifies but missed the cut. The
"How it works" view states these rules, reading the thresholds from
`config/analysis.yaml` so the explanation always matches the code.

**Audience.** The site is prepared for Crédit Agricole CIB Global Market
Research: a deep-green header and a "Prepared for…" line set in plain text.
No logo or official branding is used, and every page states that it is a
candidate assessment, not an official publication (`config/site.yaml`).

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
5. ~~Weekly fetch / monthly publish with GitHub Actions, deployed to GitHub Pages~~
