# ADR-0005: The embedding model is a deployer-level setting, not user-selectable

- **Status:** Accepted (its consequences about runtime download, per-process loading and the unpinned remote-code revision are superseded by ADR-0012)
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Related: ADR-0001, ADR-0006.

## Context
A tempting extension is letting users pick their own embedding model. It was rejected for four reasons: (1) `trust_remote_code=True` executes code downloaded from the model's repository, so a user naming a model could make the server run their code; (2) vectors from different models are not comparable, so changing a model means re-embedding everything and two users in one tenant cannot use different models; (3) many models produce more than the 1536 dimensions the column holds; (4) it would reverse the 'no unnecessary vendor exposure' decision.

## Decision
The embedding model is chosen by whoever deploys the system, through `EMBEDDING_MODEL_NAME` (currently `nomic-ai/nomic-embed-text-v1.5`, computed in-process via sentence-transformers). Every row records its own `embedding_model` and `embedding_dims`, and retrieval must filter on `embedding_model`.

## Consequences
- If a changed model is ever supported, the path is: a short allowlist plus a re-embed of existing rows. Document that as the extension path.
- The `embedding_model` filter is **not in `app/rbac.py`** today. It exists only in `scripts/smoke_real_model.py`'s own query. The Phase 5 retrieval query must add it.
- The model is loaded lazily, per worker process, and `SentenceTransformer(..., trust_remote_code=True)` is **not pinned to a `revision`**: a fresh cache downloads whatever code is current. Options not yet taken: pin `revision`, bake the cache into the image with `HF_HUB_OFFLINE=1`, or vendor the model code.

## How this is checked
`test_chunk_columns_are_required` (every vector row must record its model and dimensions). The 'rows from another model are excluded' behaviour was shown by `python -m scripts.smoke_real_model` (20/20 inside Docker per the handoff), not by the pytest suite.
