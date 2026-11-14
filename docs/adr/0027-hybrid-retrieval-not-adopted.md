# ADR-0027: Hybrid retrieval (keyword + vector) was evaluated and is not being built

- **Status:** Accepted (2026-11-14)
- **Recorded:** 2026-11-14
- **Related:** Evidence: ADR-0007 (the harness). Builds on ADR-0010, ADR-0011.

## Context
In the v2 evaluation BM25 (keyword) and nomic (meaning) each found answers the other missed: either alone found 64-75% of answerable questions at k=10, together 86%. That raised the question of fusing them in `/retrieve`, which would mean a PostgreSQL full-text leg, a new migration (`0015`) and a new security-critical query.

## Decision
**Do not build it now.** `/retrieve` stays vector-only. The decision came from a rule written *before* the new results existed (Batch D, Round 3), applied by `eval/decide.py`. Hybrid had to pass all four:
1. On v2 + v3 combined, beat the better single method by at least 5 points at **both** k=5 (the app's default) and k=10.
2. Combined MRR not lower than the better single method's (strict, no tolerance).
3. No category finds more than one fewer question than the model alone.
4. On v3 alone (not seen when v2 was analysed), beat the better single method at both depths.

What was measured: reciprocal-rank fusion (constant 60) of the harness's BM25 and nomic over the frozen 112-chunk corpus, in Python. Combined set: 53 answerable questions (v2: 28; v3: 25, of which 20 were written by the assistant with the corpus visible, so not blind).

| | BM25 | nomic | hybrid |
|---|---|---|---|
| combined, k=5 | 30 | 35 | 37 (**+3.8 points**, needs +5) |
| combined, k=10 | 39 | 39 | 42 (+5.7) |
| combined MRR | 0.449 | 0.482 | 0.540 |
| v3 alone, k=5 | 18 | 15 | 19 |
| v3 alone, k=10 | 21 | 18 | 20 (**one short of BM25**) |

Conditions 2 and 3 passed; conditions 1 (at k=5) and 4 (at k=10) failed, so the rule says stop. On v2 alone hybrid was worse than nomic at k=5 (18 vs 20) and level on MRR (0.485 vs 0.489).

## Consequences
- No migration `0015`, no change to `/retrieve`, `app/rbac.py` or the schema.
- Fusion does raise the right answer's rank (MRR 0.54 vs 0.48), but does not find enough extra answers at the default depth to justify the cost. Post hoc, not part of the rule, so a lead and not a finding: at k=20 hybrid found 50 of 53 (BM25 47, nomic 43). A caller who wants a wider net can already ask for `k` up to 20.
- Limits of this evidence: 53 questions (one is about 1.9 points); the keyword leg was the harness's BM25, not PostgreSQL full-text search; the v3 questions written by the assistant probably flatter keyword search.
- Not tested: weighted fusion (for example a smaller weight on the keyword leg) and chunk context (gap #7). Questions that every method missed (C8, P15, and Y16 for nomic) suggest chunking or chunk context is the likelier lever.

## Revisit when
A variant is written down **before** it is run (for example a lower keyword weight) and judged only on new questions the author has not seen; or real usage shows callers needing deep `k`; or the corpus grows so far that k=20 covers a small share of it.

## How this is checked
`python -m eval.decide eval/results/nomic-ai_nomic-embed-text-v1.5_20261108_183850.json eval/results/nomic-ai_nomic-embed-text-v1.5_20261114_154943.json` prints the four conditions and the verdict (its logic is covered by `tests/test_eval_decide.py`).
