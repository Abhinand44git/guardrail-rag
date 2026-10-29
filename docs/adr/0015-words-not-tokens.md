# ADR-0015: Chunk sizes are measured in words, and the code says so

- **Status:** Accepted
- **Recorded:** 2026-10-18
- **Related:** Review item 8. Related: ADR-0002, ADR-0007, ADR-0014.

## Context
The chunker's `target_tokens` / `overlap_tokens` counted whitespace-separated words, and the stored label said `fixed_500_tokens`. Measured with the real tokenizer, a 500-word chunk is a median of about 943 model tokens (max about 1,588). The name was wrong, and anything reasoning from it (a context budget, a model's input window) would have been wrong too. The "4,000-token context budget" appears in the docs only; no code implements it.

## Decision
Rename to what the code does: `target_words`, `overlap_words`, `DEFAULT_TARGET_WORDS`, and the recorded label becomes `fixed_500_words`. **The chunks themselves are unchanged**: old and new chunkers were compared over every file in the frozen eval corpus, both source types and four size settings (296 combinations) and produced identical output, so the evaluation stays comparable.

Whether to size chunks by real model tokens is not decided here. It belongs with the chunk-size experiments on the evaluation harness, after the golden questions exist.

## Consequences
- Rows written before this change carry the label `fixed_500_tokens`; it means the same thing as `fixed_500_words`. No migration; no real data exists yet.
- **Docs still to correct (your copies, not the frozen eval snapshot):** `GuardRail_RAG_Analysis.md` line 99 and `GuardRail_RAG_Design.md` line 13 describe a 4,000-token context budget per `/query`. If ADR-0002 (retrieval-only) is accepted, the caller owns its context budget and those lines should be removed; until then, mark them "planned, not implemented, and counted in words, not tokens".
- Do not edit `eval/corpus/`: it is a frozen snapshot, and changing it would invalidate saved results.

## How this is checked
`tests/test_ingest_endpoints.py` asserts the stored label; the comparison against the previous chunker was run once as a one-off check (not kept as a test, since it needs the old code).
