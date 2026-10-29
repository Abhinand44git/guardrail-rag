# Architecture Decision Records

One short file per decision: context, decision, consequences, and how it is checked.

**Rules**
- An ADR is not edited after it is accepted, except to change its **Status** line.
- To change a decision, write a new ADR and mark the old one `Superseded by ADR-NNNN`.
- Each ADR ends with *How this is checked*: a test, a script, or an honest "not checkable yet".
- `GuardRail_RAG_Decision_Log.md` stays as append-only history. The ADRs and `docs/CURRENT_STATE.md` are what to read first.

| # | Decision | Status |
|---|---|---|
| 0001 | [GuardRail owns no AI model and holds no AI keys](0001-no-owned-ai-model.md) | Accepted |
| 0002 | [Phase 5 builds project memory and a retrieval-only endpoint](0002-phase-5-retrieval-only.md) | Accepted |
| 0003 | [Portfolio framing: state scope limits honestly, don't build around them](0003-portfolio-framing.md) | Accepted |
| 0004 | [No projects tables: project_id is a label, not a security boundary](0004-no-projects-tables.md) | Accepted |
| 0005 | [The embedding model is a deployer-level setting, not user-selectable](0005-embedding-model-deployer-level.md) | Accepted |
| 0006 | [Develop and deploy on Python 3.12 with a verified dependency pin set](0006-python-312-and-pins.md) | Accepted |
| 0007 | [Keep a retrieval-evaluation harness in the repo (eval/)](0007-eval-harness.md) | Accepted |
| 0008 | [Secrets are supplied at run time and never baked into images](0008-secrets-not-in-images.md) | Accepted |
| 0009 | [Keep docs and code in step: ADRs, schema tests, a definition of done](0009-docs-and-code-discipline.md) | Accepted |
| 0010 | [Rank similarity by cosine distance, never L2 or inner product](0010-cosine-distance.md) | Accepted |
| 0011 | [The retrieval endpoint returns scores and does not decide 'no answer'](0011-scores-not-no-answer.md) | Accepted |
| 0012 | [The embedding model runs in its own container, baked into the image](0012-separate-embedder-container.md) | Accepted |
| 0013 | [Accept the per-request authentication lookups; optimise only after measuring](0013-accept-three-auth-lookups.md) | Accepted |
| 0014 | [Vector padding (768 into 1536): decision deferred until after the evaluation](0014-vector-padding-deferred.md) | Superseded by 0019 |
| 0015 | [Chunk sizes are measured in words, and the code says so](0015-words-not-tokens.md) | Accepted |
| 0016 | [There is no /auth/refresh endpoint; sessions last 60 minutes](0016-no-refresh-endpoint.md) | Accepted |
| 0017 | [A person belongs to exactly one tenant; consultants use one address per client](0017-one-user-one-tenant.md) | Accepted |
| 0018 | [The token blacklist stays in PostgreSQL; expired rows are purged on logout](0018-blacklist-stays-in-postgres.md) | Accepted |
| 0019 | [Store vectors at exactly 768 numbers; no padding](0019-vectors-768-no-padding.md) | Accepted |
| 0020 | [Project memory is visible by clearance alone, and defaults to Admin-only](0020-memory-visibility-by-clearance.md) | Accepted; amended by 0023 |
| 0021 | [The retrieval and memory endpoints read the caller's role from the database, not the token](0021-live-role-for-reads.md) | Accepted; extended by 0024 |
| 0022 | [POST /retrieve returns two labelled lists with scores](0022-retrieve-contract.md) | Accepted |
| 0023 | [Authors always see their own project-memory entries](0023-authors-see-their-own-memory.md) | Accepted |
| 0024 | [Every authenticated endpoint uses the caller's current role from the database](0024-role-from-database-everywhere.md) | Accepted |

**Template:** copy any file above. Keep it under one page.
