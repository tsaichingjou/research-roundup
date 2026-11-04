# The Research Roundup: technical notes

The full design and operating notes behind the [README](README.md): what every
module does, how each source is read and where fallbacks were needed, how papers
are scored, selected and written up, how the classification was evaluated, and
how LLM quality is kept up.

- [Architecture](#architecture)
- [Running it yourself](#running-it-yourself)
- [Deployment and scheduling](#deployment-and-scheduling)
- [Setup](#setup)
- [Usage](#usage)
- [Source coverage](#source-coverage)
- [Reliability design](#reliability-design)
- [Analysis design](#analysis-design)
- [Newsletter and explorer](#newsletter-and-explorer)
- [Evaluating the classification](#evaluating-the-classification)
- [Keeping LLM quality up](#keeping-llm-quality-up)
- [What's built](#whats-built)
- [Next steps](#next-steps)

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
| `config/analysis.yaml` | LLM models, publish day, pre-filter rules, screening batch size and selection rules. |
| `src/llm.py` | Gemini REST client: JSON mode, throttling, back-off on rate limits, model fallback. |
| `src/prompts.py` | Screening and analysis prompts, including the EM / DM / Transversal definitions. |
| `src/enrich.py` | Recovers missing abstracts from OpenAlex, publisher pages or the opening of the paper's text before papers are written off. |
| `src/fulltext.py` | Finds and reads a paper's open full text (PDF or web page) for the write-ups. |
| `src/quality.py` | LLM quality over time: benchmark regression check, number grounding of write-ups, monthly health report. |
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
python main.py fetch               # ~700 papers from the 58 sources, a few minutes
python main.py publish             # screen, select, write up last month's issue, build the site
open site/index.html               # the dashboard (Windows: start site\index.html);
                                   # the newsletter is in site/issues/
```

A full first run makes roughly 70–90 Gemini calls (scoring in batches of 8,
then 5–8 write-ups), within the free tier. `python -m pytest -q` runs the test
suite without network access or API keys.

## Deployment and scheduling

`.github/workflows/pipeline.yml` runs the agent on GitHub Actions:

| When | Job | Steps |
|---|---|---|
| Every day, 05:23 UTC | Daily screening | `fetch` (with abstract recovery) → `screen` → `catch-up` → `build` (refreshes the month in progress and its provisional picks) |
| 5th of the month, 10:17 UTC (after the Gemini daily quota resets) | Monthly issue | `fetch` → `publish` (select from the month that just ended, write up, freeze the issue, build) |
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
research published 1-31 October. It is closed on 5 November rather than the
1st (`schedule.publish_day` in `config/analysis.yaml`), because some papers
reach the feeds or the OpenAlex index a few days after their publication date;
the wait lets them into their own month. A paper that appears even later joins
the November issue as a *late arrival* (counted in the newsletter's notes), so
nothing is lost. Re-running an issue later (`publish --rewrite`, or
`--month 2026-10`) keeps the same window. Until an issue is closed, the explorer
shows the month with a provisional selection, the papers that would be chosen
if the issue were made that day: the month in progress, and from the 1st to the
5th also the month that is closing.

**Safety net for the monthly run.** GitHub runs scheduled workflows late when
its servers are busy (a run set for 05:23 has started at 13:07) and may skip
one, and on the publish day Gemini's larger models can be overloaded. So every
daily run also calls `catch-up`: from the 5th to the 8th it (re)makes last
month's issue if it is missing, was made before the 5th, or contains write-ups
by a fallback model. The issue is therefore complete within a few days without
anyone pressing a button.

A write-up produced by a fallback model (when the preferred model was
overloaded or out of daily quota) is retried with the preferred model the next
time the issue is built, and kept if that fails again.

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
python main.py agreement             # two-model agreement check -> evaluation/report.txt
python main.py regression            # re-label the frozen benchmark with the current prompt and model
python main.py quality-report        # monthly LLM health report for last month's issue
python -m pytest -q                  # run tests (no network or API key needed)
```

## Source coverage

| Required in the brief | How it is covered | Channel |
|---|---|---|
| AER, Econometrica, QJE, REStud | One source per journal | Crossref API by ISSN (90-day look-back, as journals register DOIs in batches) |
| JPE | **Partial**: only papers whose abstract can be recovered (about 40% in testing); see below | Crossref API by ISSN + OpenAlex for abstracts |
| NBER | All new working papers | NBER RSS |
| BPEA | **None so far**: spring and autumn editions, but see below | OpenAlex API by ISSN (1-year look-back; no feed exists, and Crossref returned nothing) |
| IMF | IMF Working Papers | Crossref API by ISSN (the IMF RSS endpoint returns 404) |
| BIS | BIS Working Papers | BIS RSS |
| 12 Federal Reserve regional banks + Board | One source per bank | Fed in Print RSS (the Federal Reserve System's own catalogue) |
| G10 central banks | ECB, Bank of Japan, Bank of England, Bank of Canada, Bundesbank, Banque de France, Banca d'Italia, De Nederlandsche Bank, National Bank of Belgium, Riksbank, SNB (Fed via Fed in Print) | OpenAlex API, one query per bank; BIS Central Bank Research Hub RSS as a supplement |
| European national central banks | 25 banks, from Banco de España to Norges Bank and the NBP | Same |

### Every source, in full

**Federal Reserve System** (13 sources, Fed in Print RSS): Board of Governors;
the Federal Reserve Banks of Atlanta, Boston, Chicago, Cleveland, Dallas, Kansas
City, Minneapolis, New York, Philadelphia, Richmond, San Francisco and St. Louis.

**G10 central banks and the ECB** (OpenAlex, one query per bank; BIS Central Bank
Research Hub RSS as a supplement). The G10 has eleven members; the US member,
the Federal Reserve, is covered above.

| Country / area | Central bank |
|---|---|
| Euro area | European Central Bank |
| Belgium | National Bank of Belgium |
| Canada | Bank of Canada |
| France | Banque de France |
| Germany | Deutsche Bundesbank |
| Italy | Banca d'Italia |
| Japan | Bank of Japan |
| Netherlands | De Nederlandsche Bank |
| Sweden | Sveriges Riksbank |
| Switzerland | Swiss National Bank |
| United Kingdom | Bank of England |

**European national central banks** (25, same channels):

| Country | Central bank | Country | Central bank |
|---|---|---|---|
| Austria | Oesterreichische Nationalbank | Latvia | Latvijas Banka |
| Bulgaria | Bulgarian National Bank | Lithuania | Bank of Lithuania |
| Croatia | Croatian National Bank | Luxembourg | Banque centrale du Luxembourg |
| Cyprus | Central Bank of Cyprus | Malta | Central Bank of Malta |
| Czech Republic | Czech National Bank | Norway | Norges Bank |
| Denmark | Danmarks Nationalbank | Poland | Narodowy Bank Polski |
| Estonia | Eesti Pank | Portugal | Banco de Portugal |
| Finland | Bank of Finland | Romania | National Bank of Romania |
| Greece | Bank of Greece | Russia | Bank of Russia |
| Hungary | Magyar Nemzeti Bank | Slovakia | National Bank of Slovakia |
| Iceland | Central Bank of Iceland | Slovenia | Bank of Slovenia |
| Ireland | Central Bank of Ireland | Spain | Banco de España |
| | | Türkiye | Central Bank of the Republic of Türkiye |

A few of these publish little in English or are thinly indexed, so a given month
may bring no paper from them (Bank of Greece, Banque centrale du Luxembourg, Bank
of Lithuania and Danmarks Nationalbank had none in the first weeks). Fifteen
further central banks outside Europe (Brazil, Mexico, South Africa, Korea and
others) are configured in `config/institutions.yaml` but switched off, to keep
to the brief's scope.

**Fallback strategies by source.** The brief allows a source to be removed for
a cycle when abstracts are unavailable, provided the fallbacks are documented.
Rather than drop a whole source, the agent handles each paper whose feed gives
no abstract in turn: it looks the abstract up in OpenAlex, then on the
publisher's page, then reads the opening of the paper's own text (the abstract
section of a PDF, or the first paragraphs of a speech or short article). A paper
for which all three fail is, in effect, removed from that cycle: it is labelled
by market focus from its title so it still shows in the coverage figures, but it
is not scored and cannot be selected. The sources that needed a fallback, for
all papers collected up to 4 November 2026 (`python main.py fallbacks` prints
the current table; each newsletter carries the table for its own month):

| Source | Papers | OpenAlex | Publisher page | Paper's own text | Title only |
|---|---:|---:|---:|---:|---:|
| Journal of Political Economy | 22 | 5 | – | – | 17 |
| Federal Reserve Bank of St. Louis | 26 | – | – | 13 | 7 |
| Bank for International Settlements (BIS) | 26 | 2 | – | 7 | – |
| Federal Reserve Bank of Dallas | 27 | – | – | 1 | 7 |
| Board of Governors of the Federal Reserve System | 28 | – | – | 6 | – |
| Federal Reserve Bank of Minneapolis | 24 | 1 | – | – | 4 |
| Federal Reserve Bank of New York | 25 | 2 | – | 1 | 2 |
| Federal Reserve Bank of Boston | 25 | – | – | 2 | – |
| Federal Reserve Bank of Kansas City | 25 | 1 | – | 1 | – |
| Federal Reserve Bank of Philadelphia | 26 | – | – | 2 | – |
| Deutsche Bundesbank | 23 | – | – | 1 | – |
| European Central Bank | 21 | – | – | 1 | – |
| European Systemic Risk Board | 1 | – | – | 1 | – |
| Federal Reserve Bank of Cleveland | 28 | – | – | 1 | – |
| Other central bank (doi.org) | 1 | 1 | – | – | – |

Most fallbacks concern Fed speeches and blog posts and BIS papers, whose feeds
carry no abstract but whose text is open; the only source largely removed is
JPE (below). The other 40 sources needed no fallback.

**Why BPEA shows no papers.** BPEA publishes two editions a year and offers
no feed, and Crossref returns none of its recent papers. The OpenAlex query
(both ISSNs, with a fallback by journal name, over a full year) runs in every
fetch but has so far returned no records, so the source currently contributes
nothing; it is logged as "0 items" rather than failing silently, and will pick
the papers up once OpenAlex indexes them. Reading brookings.edu directly would
close the gap; it was left out to keep every source on a documented API or feed.

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

**Pre-filter.** Non-research items (comments, replies, errata, editorial
notices, data deposits) are excluded before any LLM call. Papers still without
a usable abstract after the look-ups described above are not scored; they get a
market-focus label from their title only and cannot be selected. Every
exclusion is recorded with its reason, and `analyse` prints coverage per source,
which documents where fallbacks were needed.

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

The EM / DM / Transversal labels are not taken on trust (`src/evaluation.py`).

**Two-model agreement (the reported check).** `python main.py agreement` takes
a sample of scored papers, stratified by the screening model's label (about 35
per class, so EM is not crowded out by the much larger DM group), and has a
second, different model label them again, independently, from the same prompt
and inputs (the stronger write-up model when its small free-tier quota allows,
otherwise another lite model; the report names it). `evaluation/report.txt` gives the agreement rate with a 95%
interval, Cohen's κ, agreement per class, the same rate re-weighted to the
class mix of all screened papers, the confusion matrix, and how far apart the
two models' relevance scores are. Run it from the Actions tab (mode
*evaluate*); results are committed to `evaluation/`.

**Result (4 Nov 2026, 105 papers, gemini-3.5-flash-lite vs gemini-3.1-flash-lite):**
96.2% agreement (95% CI 91-99%), Cohen's κ 0.94; 93.8% re-weighted to the full
class mix. EM agreed on all 35 papers; the 4 disagreements are all DM vs
Transversal (e.g. a US-tariff trade-diversion paper about the EU), the boundary
that the definitions leave most open to judgement. Relevance scores were within
one point for 87% of papers.

Agreement is not accuracy: two models can make the same mistake, and models
from one family may share a bias. What it does show is how stable the labels
are and which classes get confused, and it narrows human review to where it
matters: `evaluation/disagreements.csv` lists every paper on which the models
disagree, with both models' reasons and an empty `human_label` column.

**Accuracy against human labels (ready, not yet run).** Measuring accuracy
needs a reviewer with an economics background. The tooling is in place:
`export-labels` writes a blind, stratified sample (titles and abstracts only, no
model labels, so the reviewer is not anchored), `evaluation/LABELLING_GUIDE.md`
gives the rules, and `evaluate` reports accuracy with a Wilson 95% interval,
per-class precision / recall / F1, macro-F1 and κ, on a dev / test split so
that prompt tuning on `dev` errors does not inflate the `test` score. The
cheapest first step is to label the disagreements file: where the two models
agree, a shared error is less likely.

## Keeping LLM quality up

The agreement check above shows the classification is sound today. Three
automatic layers (`src/quality.py`) keep it that way as prompts, models and
sources change, without a reviewer in the loop:

**1. Regression check when the system changes.** The 105 papers of the first
agreement check are frozen in `evaluation/benchmark.csv`, each with the label
both models agreed on (the 4 disputed papers carry none and do not count). A
second workflow, `quality-check.yml`, runs whenever `src/prompts.py`,
`config/analysis.yaml` or the benchmark changes: it re-labels the benchmark with
the new prompt and screening model and compares. Below 88% agreement it opens a
GitHub issue listing the papers whose label changed, so a prompt edit or a model
retired by Google cannot quietly change how papers are classified. The latest
result is kept in `evaluation/regression.md`. Cost: about 14 lite-model calls
per change. The threshold allows for run-to-run noise: re-running the unchanged
setup gave 93.1% (4 Nov 2026), since a model does not label borderline papers
identically every time; a real regression shows up as a larger drop.

**2. A health report for every issue.** After an issue is published (or remade
by the catch-up), `quality-report` writes a short report in English and the
workflow posts it as a GitHub issue, one per month, which GitHub emails to the
repository owner; reports are also kept in `evaluation/quality/`. It needs no
extra LLM calls and flags:

| Check | Alert when |
|---|---|
| Classification consistency | the screening and writing models disagree on more than a quarter of the selected papers |
| Market-focus mix | a class's share moves more than 15 points from the previous three months, or EM drops to zero |
| Relevance scores | the month's mean moves more than one point from the previous three months |
| Write-up model | any write-up was produced by a fallback model |
| Numbers checked against the source | any number in a write-up is not in the paper's text (layer 3) |
| Screening coverage | more than 10% of the month's papers were left unscreened (LLM failures) |

The full-text share is reported for information. Mix and score comparisons are
skipped when either side has fewer than 30 papers.

**3. Number grounding in every write-up.** Key takeaways quote estimates, and
a wrong figure is the most damaging error a summary can make. Every number in a
summary or takeaway must appear in the text the model was given (the full text
or the abstract; "10" also matches "ten"). If one does not, the model is asked
once to rewrite without it; numbers still unverified are stored with the
write-up and listed in the monthly report. This runs as part of each write-up,
at the cost of one extra call only when a number fails.

## What's built

- ✅ Ingestion of every source in the brief: RSS, Crossref and OpenAlex, with abstract recovery
- ✅ LLM analysis: relevance score, summary, key takeaways, why it matters, EM / DM / Transversal with rationale, written from the full text where open
- ✅ Responsive, email-ready HTML newsletter and an interactive dashboard (papers, source coverage, how it works)
- ✅ Daily screening and monthly publishing on GitHub Actions, deployed to GitHub Pages, with a catch-up safety net
- ✅ Classification evaluation (two-model agreement) and automatic LLM quality monitoring

## Next steps

With more time, in rough order of value:

1. **Accuracy against expert labels.** Have an economist label the blind sample
   (`export-labels`), starting with the 4 disagreements, to measure accuracy
   rather than agreement.
2. **Email delivery.** The newsletter HTML is already email-safe; sending it
   needs only the firm's mail service and a distribution list.
3. **BPEA from brookings.edu** while OpenAlex has no recent records.
4. **Wider EM coverage**: central banks of large emerging economies (Brazil,
   India, Mexico, South Africa and others), configured but switched off to keep
   to the brief's scope.
5. **Reader feedback** (which papers are opened or rated useful) to tune the
   relevance scale and selection rules.
