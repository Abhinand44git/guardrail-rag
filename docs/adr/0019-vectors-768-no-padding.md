# ADR-0019: Store vectors at exactly 768 numbers; no padding

- **Status:** Accepted (supersedes ADR-0014)
- **Recorded:** 2026-10-25
- **Related:** ADR-0005, ADR-0007, ADR-0012, ADR-0014. Migration `0013`.

## Context
ADR-0014 deferred the question of the `vector(1536)` columns (nomic's 768 numbers followed by 768 zeros) until the evaluation could say whether a different or narrower storage width was worth it. The evaluation (`python -m eval.run ... --dims 768,512,256,128`, one embedding pass, every width cut from the same vectors with nomic's own recipe: layer-norm, cut, rescale) gave, on 28 answerable questions:

| Width | recall@10 | MRR | Questions that flipped vs full width |
|---|---|---|---|
| 768 (full) | 75% | 0.49 | none |
| 512 | 71% | 0.50 | 1 lost |
| 256 | 75% | 0.49 | 1 gained, 1 lost |
| 128 | 75% | 0.47 | 2 gained, 2 lost |

One question is about 3.6 points and the harness treats gaps under about 10 points as noise, so this is a **tie**: nothing measurable is gained or lost by cutting. On 112 chunks that says little about a larger corpus, so it does not justify a narrower width either.

## Decision
`document_vectors.embedding` and `project_memory.embedding` are `vector(768)`: exactly the model's native width. No padding. The application **refuses** a vector of any other width (`app/embeddings.py::require_storage_width`); it never pads or cuts one. A model with a different width needs a migration and a re-embed, which ADR-0005 already said a model change costs.

Narrower storage (256) stays a documented option: 768-wide vectors can be cut later without re-running the model, but 256-wide vectors can never be widened.

## Consequences
- Storage and scan cost halve compared with the padded layout; cosine ranking is unchanged (zeros never affected it).
- Migration `0013` cuts existing rows in place with no re-embedding, **and refuses to run** if any stored vector has a non-zero value past position 768 (it would mean a wider model; cutting it would destroy data). It uses a `real[]` round-trip, not pgvector's `subvector()`, so it runs on pgvector 0.6 as well as 0.8.
- Tested on pgvector **0.6.0** only; the compose image's 0.8.x is unverified for this migration.

## How this is checked
`tests/test_schema.py::test_embedding_columns_are_vector_768` and `test_a_vector_of_the_wrong_width_is_refused_by_the_database`; `tests/test_embedder_service.py::test_a_model_of_the_wrong_width_is_refused_not_padded_or_cut`; `tests/test_migration_0013.py` runs the real `alembic upgrade/downgrade` on a throwaway database with real padded rows (lossless cut, lossless round trip, refusal that changes nothing). The width evaluation is reproducible from `eval/run.py --dims`.
