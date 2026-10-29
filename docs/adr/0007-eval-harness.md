# ADR-0007: Keep a retrieval-evaluation harness in the repo (eval/)

- **Status:** Accepted
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Related: ADR-0011.

## Context
112 tests proved correctness; none measured whether the right chunk comes back. A system can pass every correctness test and still be useless. This was the second serious problem in the senior review.

## Decision
Keep `eval/` as a quality regression test, run with `python -m eval.run --model <name>`. Design: a hand-written golden set (26 questions today: 23 answerable, 3 unanswerable) over a **frozen** corpus snapshot (112 chunks, 36 files) so runs are comparable; answers are matched by **anchor text**, not chunk ids, so chunk-size experiments do not invalidate the set; metrics are recall@k and MRR; a BM25 keyword baseline is the number the model must beat.

## Consequences
- Evidence so far (nomic-embed-text-v1.5, first run 2026-10-10): recall@5 43% (BM25) → 61%; at k=10 it is a **tie at 65%**, so the real advantage is ranking the right chunk higher, not finding it. On the owner's own five questions (Y1–Y5) BM25 hits 0/5 and nomic 4/5 at k=10. Each method finds answers the other misses (union 20/23 at k=10), which is the evidence for considering hybrid search. Hybrid search is **not decided**.
- Caveats: one question is about 4 points of recall; the corpus is small, so scores flatter the model; the AI assistant that helped build the set wrote most anchors and the paraphrase questions with the docs in view; anchor judging is strict, so some misses may be valid answers.
- Next: the owner rewrites some questions in their own words and adds more, then misses are hand-judged.

## How this is checked
`python -m eval.check_golden` (every anchor exists; BM25 baseline) and `python -m eval.run`. Results are saved under `eval/results/`.
