# ADR-0014: Vector padding (768 into 1536): decision deferred until after the evaluation

- **Status:** Superseded by ADR-0019 (2026-10-25)
- **Recorded:** 2026-10-18
- **Related:** Review item 5. Related: ADR-0005, ADR-0007, ADR-0012.

## Context
Vectors are 768-wide but stored in `vector(1536)` columns, zero-padded. Cosine ranking is unaffected, but storage and scan cost double. A real tradeoff exists: changing the column width later costs a migration and a re-embed, which is cheapest now while there is no real data. But the right width depends on questions the evaluation has not answered yet: whether another model (with a different width) beats nomic on this project's own data, and whether nomic's Matryoshka truncation (its model card reports benchmark scores of 62.28 at 768 dims, 61.96 at 512, 61.04 at 256 and 59.34 at 128) is good enough to store narrower vectors.

## Decision
Do **not** change the column now. Keep `vector(1536)` with zero-padding, and decide after the golden questions are in and the model/truncation experiments have been run.

Options to choose between then: (a) keep 1536 and accept the cost; (b) migrate to the chosen model's real width; (c) migrate to a truncated width if the evaluation shows the quality loss is acceptable.

## Consequences
- Padding stays inside the API (`EmbeddingService`) and the embedder returns native-width vectors (ADR-0012), so deferring costs nothing structurally.
- **Do not ingest real data before deciding**: a width change means re-embedding it. (A model change would force that anyway.)
- Any migration must update `tests/test_schema.py` deliberately, which is intended: the width is pinned there.

## How this is checked
`tests/test_schema.py`: `test_embedding_columns_are_vector_1536` and `test_a_native_768_dim_vector_is_refused_so_zero_padding_is_mandatory` pin today's behaviour.
