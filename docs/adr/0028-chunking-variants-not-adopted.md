# ADR-0028: Content-aware chunking and chunk headers were evaluated and are not being adopted

- **Status:** Accepted (2026-11-14)
- **Recorded:** 2026-11-14
- **Related:** ADR-0015 (chunk size in words), ADR-0007 (the harness), ADR-0027 (the earlier, same-style experiment on hybrid search)

## Context
Chunks are cut every 500 words (code: on line boundaries) whatever they contain. Two questions that every method missed showed why that looked costly: C8's answer sat in a 411-word chunk holding three functions that started inside an unrelated comment and never named its file, and P15's answer sat in a 500-word chunk that began mid-sentence with no heading. The hypothesis: chunks that follow the content, and/or carry a header, would be found more often.

## Decision
**Keep fixed 500-word chunks with 50 words of overlap.** `app/chunking.py` and the stored data do not change. The decision came from a rule written and committed *before* any result existed (`eval/round5_plan.md`), applied by `eval/decide_chunking.py`. A variant had to pass all five: at least +5 points of recall at both k=5 and k=10; MRR not lower; no category losing more than one question; v2 alone and v3 alone each at least as good as today's; and the top-5 chunks still carrying at least half of today's text.

Variants (embedding model, revision and prefixes unchanged; only the chunking differs): `A_header` (same cuts, `File: <path>` added before embedding), `B_structure` (Python cut by function or class, Markdown by heading section, units under 60 words merged, none over 500), `C_structure_header` (B's cuts plus a header with the path and the function or section name).

On the 53 answerable questions (golden v2 + v3), nomic only, today's chunking finds 35 at k=5 and 39 at k=10:

| | k=5 | k=10 | MRR | rule |
|---|---|---|---|---|
| A_header | 36 (+1.9 points) | 40 (+1.9) | 0.548 | fails 1 (recall) |
| B_structure | 33 (-3.8) | 40 (+1.9) | 0.537 | fails 1, 3 (broad 10 -> 7 at k=10) and 4 (v2 alone 17 vs 20 at k=5) |
| C_structure_header | 36 (+1.9) | 40 (+1.9) | 0.575 | fails 1 and 4 (v2 alone 19 vs 20 at k=5) |

Today's MRR is 0.482. The verdict was reproduced by `python -m eval.decide_chunking` on 2026-11-14 and matched the hand-worked numbers above. Condition 5 (average words in the top-5 chunks; today's 1,995) passed for all three, narrowly for the structural cuts: A 2,012 (101%), B 1,039 (52%), C 1,054 (53%). Smaller chunks give the caller about half the text.

## Consequences
- No code, schema or data change; no re-ingestion.
- Post hoc, and not part of the rule, so a lead and not a finding: the right chunk came first far more often with a header or content-aware cuts (rank 1 for 18 of 53 questions today, 24 for A and B, 25 for C; MRR 0.48 up to 0.55-0.58), but the number of questions found in the top 5 or 10 barely moved. The change improved *ordering*, not *finding*. `A_header` is the cheapest variant and carries the least risk.
- B's smaller chunks hurt broad, multi-part questions (they spread one answer across several chunks).
- Limits: 53 questions (one is about 1.9 points); three variants were tried on questions already used for ADR-0027; part of v3 was written with the corpus visible; chunk counts differ (112 vs 207), so "rank of the first correct chunk" is not perfectly like for like.

## Revisit when
A variant, for example `A_header`, is judged by a rule written first and aimed at what it actually changes (top-1 hits and MRR), on fresh questions the author has not seen; or real use shows callers reading only the top one or two results.

## How this is checked
`python -m eval.decide_chunking <v2 baseline result> <v3 baseline result> <A result> <B result> <C result>` (results in `eval/results/`, runs of 2026-11-14; the logic is covered by `tests/test_eval_decide_chunking.py` and the chunkers by `tests/test_eval_chunkers.py`).
