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
| `config/institutions.yaml` | The central banks covered: name, group, OpenAlex aliases and web domains. |
| `src/models.py` | `Paper` data model, stable IDs and title normalisation. |
| `src/fetchers/` | One module per source *type*: `rss.py` for any RSS/RDF/Atom feed, `crossref.py` for journals with an ISSN (Crossref API), `openalex.py` for works by institution or ISSN (OpenAlex API), `http.py` for shared download/retry logic. |
| `src/storage.py` | SQLite storage with de-duplication by URL **and** by normalised title, plus a `fetch_log` table recording every run. |
| `src/pipeline.py` | Runs all enabled sources; a failing source is logged and skipped, never stops the run. |
| `config/analysis.yaml` | LLM models, time window, pre-filter rules, screening batch size and selection rules. |
| `src/llm.py` | Gemini REST client: JSON mode, throttling, back-off on rate limits, model fallback. |
| `src/prompts.py` | Screening and analysis prompts, including the EM / DM / Transversal definitions. |
| `src/enrich.py` | Recovers missing abstracts from OpenAlex and publisher pages before papers are written off. |
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
and the rest are listed in the explorer as "not assessed: no abstract
available" and counted in each newsletter's "How this issue was made" note.
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
records for JPE, some BIS and Fed in Print items). After every fetch the agent
tries two open sources before giving up on a paper (`src/enrich.py`):
**OpenAlex**, a free scholarly index, by DOI or near-exact title; then the
abstract metadata tags (`citation_abstract`, `dc.description`, `og:description`)
on the paper's own page, which publishers expose even when the full text is
paywalled. No paywall is bypassed and no full text is downloaded. The source of
each recovered abstract is stored and shown in the explorer and the newsletter.
Papers still without an abstract are listed but not assessed, as agreed for
paywalled sources, and counted per source in every issue. Fed in Print feeds
also include speeches and articles, which are excluded when they lack an
abstract or screened out as low relevance.

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

**"In the newsletter"** marks the 5–8 papers chosen for an issue: the highest
relevance scores, at least 6/10, no more than two per source, with an
emerging-markets paper swapped in when one qualifies but missed the cut. The
explorer states these rules next to each issue, reading them from
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
5. Weekly fetch / monthly publish with GitHub Actions, deployed to GitHub Pages
