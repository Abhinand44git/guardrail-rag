# ADR-0010: Rank similarity by cosine distance, never L2 or inner product

- **Status:** Accepted
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Related: ADR-0005, ADR-0011.

## Context
The smoke test measured the stored real vectors: they are **not unit length** (length about 20.3). For unit vectors, L2 distance, inner product and cosine all give the same ordering; for these they do not, so a ranking by `<->` or `<#>` would return a different, worse order. Zero-padding to 1536 does not change cosine similarity.

## Decision
Phase 5's retrieval query orders by cosine distance (`cosine_distance` in SQLAlchemy, `<=>` in SQL).

## Consequences
- The query must also filter on `embedding_model` (ADR-0005). Neither the cosine ordering nor that filter lives in `app/rbac.py`, which encodes only the access predicate. `scripts/smoke_real_model.py` is the only place that combines all three today.
- If a vector index is ever added it must use the cosine operator class, or it will not be used for this ordering.

## How this is checked
Not checkable yet: no retrieval query exists in `app/`. `scripts/smoke_real_model.py` already ranks this way. `test_no_vector_index_exists_yet` is the tripwire for the index decision.
