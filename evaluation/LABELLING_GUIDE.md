# Labelling guide — market focus

Fill the `human_label` column of `labels.csv` with **EM**, **DM** or
**Transversal** (`T` is accepted). Use only the title and abstract, and apply the
same definitions the LLM is given (`src/prompts.py`):

| Label | Use when |
|---|---|
| **EM** | The data, setting or conclusions are primarily about emerging or developing economies (e.g. China, India, Brazil, Mexico, Turkey, South Africa, Latin America, Sub-Saharan Africa, frontier / low-income countries, EM sovereign spreads, capital flows to EMs). |
| **DM** | Primarily about advanced economies (US, euro area or members, UK, Japan, Canada, Australia, Switzerland, Nordics). A study using only US data is DM even if the mechanism is general. |
| **Transversal** | Relevant to both groups or to neither specifically: cross-country panels mixing advanced and emerging economies, global phenomena (global financial cycle, commodity prices, world trade), theory or methods papers that name no particular economy. |

Rules

1. Label from what the abstract **states**, not from the authors' institution or
   the type of model used.
2. Label **before** looking at any LLM output (the file deliberately contains none).
3. If you hesitate between two labels, pick the better one and write the reason in
   `notes` — ambiguous cases are discussed in the evaluation write-up.
4. Leave `human_label` empty to skip a paper.

`split`: label every row. Rows marked `dev` may be inspected to improve the
prompt; rows marked `test` are only used for the final reported score.
