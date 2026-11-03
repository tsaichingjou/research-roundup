"""Prompts for the two LLM stages.

Stage 1 (screening) is cheap and runs on every candidate in batches.
Stage 2 (deep analysis) runs only on the 5-8 selected papers.
"""

SYSTEM = """You are a senior economist in the Global Market Research team of a large \
European corporate and investment bank. You curate "The Research Roundup", a monthly \
newsletter that brings the most market-relevant new academic and central-bank research \
to economists, rates/FX/credit strategists, and sales & trading colleagues.

You are rigorous and concise. You never invent findings, numbers, countries or methods \
that are not stated in the title or abstract you are given. If the abstract is vague, \
say less rather than guess.

House style: measured and factual, like a bank research note. Avoid promotional \
intensifiers such as "vital", "crucial", "critical", "highly relevant", "groundbreaking" \
or "must-read"; let the content show why a paper matters. Do not claim a paper is useful \
to a specific trading desk unless the link is direct."""

MARKET_FOCUS_GUIDE = """Market-focus classification (exactly one label):
- "EM"  (Emerging Markets): the paper's data, setting or conclusions are primarily about \
emerging or developing economies — e.g. China, India, Brazil, Mexico, Indonesia, Turkey, \
South Africa, Latin America, Sub-Saharan Africa, frontier or low-income countries, EM \
sovereign spreads or capital flows to EMs.
- "DM"  (Developed Markets): primarily about advanced economies — e.g. the United States, \
euro area or its members, United Kingdom, Japan, Canada, Australia, Switzerland, the \
Nordics. A study using only US data is DM even if the mechanism is general.
- "Transversal": relevant across both groups, or not specific to either — cross-country \
panels mixing advanced and emerging economies, global phenomena (global financial cycle, \
commodity prices, world trade), or theory/methods papers with no particular geography.
Judge by where the evidence and conclusions apply, not by the authors' institution.
Base the label only on countries, regions or data the title/abstract actually names. Do not infer geography from the type of model (e.g. "New Keynesian models are used for advanced economies") or from the policy debate the paper might feed into: a theory paper that names no country is "Transversal"; it is "DM" or "EM" only if the abstract applies or calibrates it to specific economies of that group."""

RELEVANCE_GUIDE = """Market relevance score (0-10) for a markets research audience:
- 9-10: directly informs views on monetary policy, inflation, rates, FX, sovereign or \
corporate credit, banking/financial stability, capital flows, or asset prices; timely and \
with clear, usable conclusions.
- 6-8: macro-financial, fiscal, trade, labour-market or growth research with clear \
implications for the macro outlook or markets.
- 3-5: solid economics with only indirect market implications (e.g. sectoral or micro \
studies with some macro angle).
- 0-2: little or no relevance to markets (e.g. health, education, history, pure \
methodology without application), or not a research paper (comment, editorial)."""


def screening_prompt(papers: list[dict]) -> str:
    lines = [
        "Screen the following new publications for possible inclusion in this month's issue.",
        "",
        RELEVANCE_GUIDE,
        "",
        MARKET_FOCUS_GUIDE,
        "",
        "Return a JSON array with one object per paper, in any order, with exactly these keys:",
        '  "id": the paper id as given,',
        '  "score": integer 0-10,',
        '  "market_focus": "EM" | "DM" | "Transversal",',
        '  "topic": short topic label, 1-4 words (e.g. "Monetary policy", "Sovereign debt"),',
        '  "reason": one sentence explaining the score.',
        "",
        "Papers:",
    ]
    for p in papers:
        lines += [
            "---",
            f"id: {p['id']}",
            f"source: {p['source']}",
            f"title: {p['title']}",
            f"abstract: {p['abstract']}",
        ]
    return "\n".join(lines)


def title_prompt(papers: list[dict]) -> str:
    """Market-focus label for publications with no abstract: title and source only.
    No relevance score is asked for, since a title is too little to rank on."""
    lines = [
        "These publications have no abstract available. From the title and the publishing",
        "institution alone, give each one a market-focus label and a short topic.",
        "",
        MARKET_FOCUS_GUIDE,
        "",
        "Return a JSON array with one object per publication, with exactly these keys:",
        '  "id": the id as given,',
        '  "market_focus": "EM" | "DM" | "Transversal",',
        '  "topic": short topic label, 1-4 words,',
        '  "reason": one short sentence on why this label fits the title.',
        "",
        "Publications:",
    ]
    for p in papers:
        lines += ["---", f"id: {p['id']}", f"source: {p['source']}", f"title: {p['title']}"]
    return "\n".join(lines)


def analysis_prompt(paper: dict) -> str:
    authors = ", ".join(paper.get("authors") or []) or "not listed"
    full_text = paper.get("full_text")
    if full_text:
        material = f"""Abstract: {paper['abstract']}

Full text (may be truncated; references removed):
<<<
{full_text}
>>>"""
        basis = "the paper (abstract and full text)"
        takeaway_rule = ("each a concrete finding or implication from the paper; quote the key "
                         "estimate, magnitude, sample or period where the text gives one")
        extra = """
Use the full text to be specific — the headline estimates, the data and period, and what
the authors conclude for policy or markets. Use only numbers that appear in the text,
and do not describe tables or figures you cannot see."""
    else:
        material = f"Abstract: {paper['abstract']}"
        basis = "the abstract"
        takeaway_rule = "each a concrete finding or implication stated in the abstract"
        extra = ""
    return f"""Write the newsletter entry for this paper.

Source: {paper['source']}
Title: {paper['title']}
Authors: {authors}
{material}
{extra}
{MARKET_FOCUS_GUIDE}

Return one JSON object with exactly these keys:
  "summary": plain-language summary for a non-specialist finance professional, 70-110 \
words, no jargon without explanation, no hype;
  "key_takeaways": array of exactly 3 short bullet strings (max 30 words each), \
{takeaway_rule};
  "relevance": 50-90 words on why this matters for markets research — which asset \
classes, policy debates or economies it informs, and how a strategist might use it. Be \
specific but do not overstate;
  "market_focus": "EM" | "DM" | "Transversal";
  "market_focus_rationale": 1-2 sentences justifying the label from the paper's data, \
setting or scope;
  "regions": array of the countries/regions the paper studies (empty if none stated);
  "topics": array of 1-3 topic tags (e.g. "Monetary policy", "Inflation", "Trade");
  "confidence": "high" | "medium" | "low" — how well {basis} supports the analysis."""


def intro_prompt(papers: list[dict]) -> str:
    listing = "\n".join(f"- [{p['market_focus']}] {p['title']} ({p['source']}): {p['summary']}"
                        for p in papers)
    return f"""Write the editor's note that opens this month's issue of The Research Roundup.

Featured papers:
{listing}

Return one JSON object: {{"intro": "..."}} where intro is 2-3 sentences (max 70 words)
naming the one or two themes that connect several of these papers and why they matter
for markets now. Do not list every paper, do not add facts that are not in the
summaries, and keep the measured house style."""
