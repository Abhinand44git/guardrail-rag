# GuardRail RAG — current state

Rewrite this page each phase. The Decision Log is append-only history; decisions live in [`docs/adr/`](adr/README.md). Where this page and an older doc disagree, this page was checked against the code more recently.

**Last verified:** 2026-10-28, in the real Docker stack (compose, `pgvector/pgvector:pg16` image running pgvector 0.8.x, embedder container healthy, new port binding `127.0.0.1:5433->5432`).
**How:** on a **fresh volume**: `alembic upgrade head` (`-> 0001` through `0013`, clean), `python -m scripts.seed` (seeded), `docker compose exec api pytest -q` → **319 passed, 3 skipped** (322 tests; the three skips are the tests that read `docker-compose.yml` or the Dockerfile, which are not copied into the API image). The same suite also passed, once, against an **existing** database that was still at revision `0003` with old seed rows, which upgraded through `0013` without error. Earlier the same suite passed 322 on bare-metal Postgres 16 + pgvector 0.6.0 (2026-10-25: the 215 from before Phase 5, plus 107 new or rewritten: 87 in `test_retrieve_endpoint.py` and `test_project_memory_endpoints.py`, 8 in `test_role_is_read_from_the_database.py`, 5 in `test_migration_0013.py`, 6 in `test_schema.py`, 1 compose test).
**Not exercised in Docker:** `POST /retrieve` and `POST /project-memory` through the real embedding model (the tests use a stand-in embedder behind the same interface); everything else Phase 5 added is covered by the run above.

## What exists (the route table is pinned by `tests/test_api_surface.py`)

| Area | Routes |
|---|---|
| Auth (users) | `POST /auth/login`, `POST /auth/logout` |
| Operator | `POST /operator/login`, `POST /operator/logout`, `POST /operator/tenants`, `PATCH /operator/tenants/{id}`, `POST /operator/tenants/{id}/admin` (first Admin only) |
| Admin | `POST/GET /admin/users`, `PATCH/DELETE /admin/users/{id}` (DELETE is a soft deactivation) |
| Ingest | `POST /ingest/code`, `POST /ingest/document`, `PATCH/DELETE /ingest/document/{id}` (Admin only; new documents default to Admin-only) |
| Project memory | `POST /project-memory` (Admin, Developer), `GET /project-memory` (all roles): you see an entry if it is in your tenant and either your clearance allows it or you wrote it; entries default to Admin-only ([ADR-0020](adr/0020-memory-visibility-by-clearance.md), [ADR-0023](adr/0023-authors-see-their-own-memory.md)) |
| Retrieval | `POST /retrieve` (all roles): two ranked lists, document chunks and memory entries, each with a cosine score; no answer text, no model call ([ADR-0022](adr/0022-retrieve-contract.md)) |
| Other | `GET /health` |

## Containers

`api` (FastAPI, no ML libraries) → `db` (PostgreSQL + pgvector), and `api` → `embedder` over a private `internal` network with no internet and no published port. The embedder image contains the model and its remote code at commits pinned in `embedder/revisions.lock`, fetched only at build time and proven to load offline during the build ([ADR-0012](adr/0012-separate-embedder-container.md)).

## What does not exist (checked)

- **No answer generation, by design.** `/retrieve` returns evidence; the caller's own model writes the answer ([ADR-0001](adr/0001-no-owned-ai-model.md), [ADR-0002](adr/0002-phase-5-retrieval-only.md)). No score cut-off ([ADR-0011](adr/0011-scores-not-no-answer.md)).
- **No way to reclassify or delete a memory entry** once saved (an author always sees their own, but nobody can change their level or remove them). Owner's decision ([ADR-0020](adr/0020-memory-visibility-by-clearance.md), [ADR-0023](adr/0023-authors-see-their-own-memory.md)).
- **No merged ranking** of documents and memory: the two scores are not comparable ([ADR-0022](adr/0022-retrieve-contract.md)).
- **No `/auth/refresh`, by design.** It was only ever written in the Analysis doc ([ADR-0016](adr/0016-no-refresh-endpoint.md)). Sessions last 60 minutes.
- **No `user_llm_config`, `query_log`, `projects` or `project_members` tables** (ADR-0001, ADR-0004).
- **No vector index.** Every search is an exact scan (a test fails if one appears).
- **No self-service password change.** Temporary passwords are emailed and never expire.

## Schema

Seven tables plus `alembic_version` (vectors are `vector(768)`, no padding: [ADR-0019](adr/0019-vectors-768-no-padding.md); `project_memory` has `min_role_required`): `operators`, `tenants`, `users`, `documents`, `document_vectors`, `project_memory`, `token_blacklist`. The schema comes from `alembic/versions/`; `tests/test_schema.py` states and checks what the docs claim about it. `models.py` and the migrations differ in 20 known, listed ways that do not change behaviour ([ADR-0009](adr/0009-docs-and-code-discipline.md)).

## Evaluation

Golden set v2 (`eval/golden_set.json`): **35 questions, 28 answerable and 7 not**. The project owner rewrote five of the assistant's questions (P1, P3, P4, P5, P8) and added nine of their own (Y8–Y12, U4–U7); the original set is kept as `golden_set_v1.json`. Anchors were verified against the chunked corpus (`python -m eval.check_golden`: all found). **BM25 baseline on v2:** recall@5 43%, recall@10 64%, essentially unchanged from v1 (43% / 65%): rewording the five questions did not break keyword search, so leakage from the assistant's wording was a small effect. **nomic on v2 (run 2026-10-25, revision `e9b6763…`):** recall@10 **75%** (BM25 64%), MRR **0.49** (BM25 0.38): about three questions better, right at the harness's own noise line (one question = 3.6 points). Nomic ranks better and is far better on broad and code questions; BM25 is better on paraphrase (partly because the assistant wrote those questions with the docs in view). Together they find 24 of 28 answerable questions (86%): each finds answers the other misses, which is the evidence for hybrid search (#6). Still missed by both: P5, P7, C2, Y3. A similarity cut-off cannot detect "no answer" (unanswerable up to 0.713; correct rank-1 answers as low as 0.610). **Width test** (same vectors cut to 768/512/256/128): a tie within noise, so storage stays at 768 ([ADR-0019](adr/0019-vectors-768-no-padding.md)). Caveats: 35 questions, a 112-chunk corpus that flatters scores, and the corpus is the pre-ADR snapshot (ask nothing about the embedder container, the blacklist purge, or anything built after it). P4, P5, P8 are still misses: worth hand-judging whether the top chunk is a valid answer ([ADR-0007](adr/0007-eval-harness.md)).

## Known gaps, by review number

| # | Gap | State |
|---|---|---|
| 5 | Zero-padding to 1536 doubles storage and scan cost | **Fixed**: 768, no padding, migration `0013` ([ADR-0019](adr/0019-vectors-768-no-padding.md)) |
| 6 | Dense-only search; BM25 and nomic each find answers the other misses | **Evidence in** (union 86% vs 75% / 64%); hybrid not built: Batch D |
| 7 | Fixed-size chunking, no file path or title in the chunk | Not decided, after the golden questions |
| 8 | "Tokens" were really words | **Fixed** by renaming; chunks identical ([ADR-0015](adr/0015-words-not-tokens.md)). Real-token sizing belongs with #7 |
| 9 | Emailed temporary passwords, no forced change | **Open** — its own security-critical batch |
| 10 | Several DB lookups on every authenticated request | **Accepted**, optimise only after measuring ([ADR-0013](adr/0013-accept-three-auth-lookups.md)) |
| 11 | Expired `token_blacklist` rows never purged | **Fixed**: purged on logout ([ADR-0018](adr/0018-blacklist-stays-in-postgres.md)) |
| 12 | Model loaded per worker, on first use | **Fixed** (separate embedder; built and running healthy in Docker, 2026-10-28) ([ADR-0012](adr/0012-separate-embedder-container.md)) |
| 15 | `/auth/refresh` documented, never built | **Closed**: not built by design; the Analysis doc line still needs removing |
| — | One user, one tenant | **Accepted** as a documented limit ([ADR-0017](adr/0017-one-user-one-tenant.md)) |
| — | Ingestion is synchronous: about 2.9 s per chunk, so a 100-chunk upload takes about 5 minutes | Open; separating the embedder did not change this |
| — | Remote model code was downloaded at run time, unpinned | **Fixed in code**: pinned and baked at build time; the build check is yours to run. It is still third-party code you are choosing to trust |
| — | DB port published as `0.0.0.0:5432` with the default password | **Fixed 2026-10-25**: `127.0.0.1:5432:5432` in `docker-compose.yml`, with a test. The default password `guardrail` still applies if `POSTGRES_PASSWORD` is not set in `.env`: set it (and `DATABASE_URL` to match) before exposing anything |
| — | `/ingest/*` and `/admin/*` gated on the role in the token, so a demoted Admin kept Admin powers for up to 60 minutes | **Fixed 2026-10-25** in `require_user`: the role now comes from the database on every endpoint, no extra query, no schema change ([ADR-0024](adr/0024-role-from-database-everywhere.md)) |

## Needs the owner

1. Memory entries cannot be reclassified or deleted after saving. Add Admin-only `PATCH` / `DELETE /project-memory/{id}` (mirrors documents)? Should Trainees be allowed to write memory (today: no)? Then correct the Analysis doc line about "only their own" entries (now: you always see your own; everything else follows clearance).
2. Correct your live docs: `GuardRail_RAG_Analysis.md` line 46 (remove `POST /auth/refresh`); lines 99 (Analysis) and 13 (Design) on the 4,000-token budget (ADR-0015); Analysis and Design still promise `/query` with generation and vectors of 1536. Edit the stale project prompt (still says Groq and the Hugging Face Inference API). Delete the stale `HF_*` lines from your local `.env`.
3. Set a real `POSTGRES_PASSWORD` in `.env` (and `DATABASE_URL` to match).
4. Optional: one real `POST /retrieve` and `POST /project-memory` call through the real model. The Docker stack and migration `0013` are verified; this last piece is not.
5. Push the repository (nothing has been pushed).

## Not yet verified anywhere

`/retrieve` and `POST /project-memory` through the real model; embedder memory *during a large ingest* (only idle memory was measured); the production `CMD` with `--workers 2` under Docker (compose overrides it with one `--reload` worker); real SMTP (only the dev outbox is exercised).

## Re-verify

```bash
docker compose up --build -d && docker compose exec api alembic upgrade head
docker compose exec api python -m scripts.seed
docker compose exec api pytest -q                              # expect 319 passed, 3 skipped
docker compose exec api python -m scripts.smoke_real_model     # real model through the embedder, 20 checks
docker compose exec api test -f /app/.env && echo LEAK || echo env-absent
```
