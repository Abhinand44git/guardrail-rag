# ADR-0022: POST /retrieve returns two labelled lists with scores

- **Status:** Accepted
- **Recorded:** 2026-10-25
- **Related:** ADR-0002, ADR-0004, ADR-0010, ADR-0011, ADR-0020.

## Decision
`POST /retrieve` takes `query`, optional `project_id`, optional `phase` (+ `include_earlier_phases`, default true), and `k` (1..20, default 5). It returns `{embedding_model, documents: [...], memory: [...]}`, each list ranked within itself, each hit carrying its **cosine similarity score**. No score cut-off, no answer text, no model call.

- **Two lists, not one merged ranking:** a score for a ~900-token chunk and a score for a one-sentence note are not on a common scale. Merging is deferred until an evaluation shows it helps.
- **Ranking:** cosine distance only (ADR-0010), restricted to rows with the query's `embedding_model` (ADR-0005), and skipping all-zero vectors (cosine distance to one is `NaN`, which is not valid JSON).
- **`project_id` and `phase` only narrow memory.** Documents have no project or phase. Neither can widen access (ADR-0004, ADR-0020).
- **`phase`:** the named phase and every earlier one (planning, analysis, design, development, testing, deployment), unless `include_earlier_phases` is false.
- **The question is embedded with the query prefix**, stored notes with the document prefix.
- Nothing is written, and the question text is not logged (there is no `query_log` table).

## Consequences
Failure modes are safe errors (`503` embedder unavailable, `500` otherwise) with no internals in the body. No rate limiting yet (Phase 6).

## How this is checked
`tests/test_retrieve_endpoint.py` (46 tests): cosine-vs-L2 with scripted vectors, model filter, zero vectors, `k` bounds, phase/project semantics, exact response keys, prefixes, no writes, error bodies.
