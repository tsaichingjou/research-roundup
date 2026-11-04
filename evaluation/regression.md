## LLM regression check: ✅ Passed

Run 2026-11-04 15:17 UTC with `gemini-3.5-flash-lite` and the current prompt, on the frozen benchmark (`evaluation/benchmark.csv`, 105 papers; 101 with an agreed reference label).

- Agreement with the reference labels: **93.1%** (alert below 90%)
  - EM: 97.1%
  - DM: 90.6%
  - Transversal: 91.2%

Papers whose label changed (7):

- EM → DM: Hours or Heads? Demand Shocks and Labour Market Adjustments in Poland
- DM → Transversal: Teaching Safety Skills for Emergency Situations to Autistic Individuals: A Rapid Literatur
- DM → Transversal: Replication Data for: Beliefs and the Incentive Effects of Preferential College Admissions
- DM → Transversal: Why Do Americans No Longer Work So Much More Than Non-Americans?
- Transversal → DM: How New Products Enter Markets: Social Networks, Consumer Imports, and Retailer Learning
- Transversal → EM: BoC–BoE Sovereign Default Database: What’s new in 2026?
- Transversal → DM: Causal State-Dependent Local Projections

Re-run after any change to `src/prompts.py` or the models in `config/analysis.yaml`. A drop means the change altered how papers are classified: check the changed papers before keeping it.
