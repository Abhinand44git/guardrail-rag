# ADR-0009: Keep docs and code in step: ADRs, schema tests, a definition of done

- **Status:** Accepted
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Fixes review Problem 3.

## Context
The same fact lived in four markdown files, the code and the log, and nothing failed when one copy was forgotten. Seven-plus cases are on record: the Groq default surviving a doc 'fix', the token blacklist promised in Phase 2 and built in Session 7, and `project_memory`, `documents.source_type`, `document_vectors.chunk_strategy` and `tenants.is_active` described as existing before they were migrated. This session found more: `/auth/refresh` is documented but has no route, and the log says every retrieval query filters on `embedding_model` while `app/rbac.py` does not.

## Decision
1. **One source of truth per fact.** The schema comes from the migrations; docs link to it instead of restating it.
2. **Make claims checkable.** A claim a doc makes about the database gets a test in `tests/test_schema.py`. Claims about *absence* get a TRIPWIRE test that fails when someone builds the thing, which is the prompt to write an ADR.
3. **Definition of done** for a schema change: migration, `models.py`, `tests/test_schema.py`, the log, and one line saying how the claim was verified.
4. **ADRs** in `docs/adr/`: one file per decision, never edited afterward except to change its status; a new ADR supersedes an old one.
5. **`docs/CURRENT_STATE.md`**: a short page rewritten each phase. The Decision Log stays as append-only history.

## Consequences
`tests/test_schema.py` compares `models.py` to the migrated database with Alembic's own comparison. Today that finds 20 known differences (listed in the test, grouped by kind); a new difference fails the test, and so does a listed one that stops being true. The differences are not fixed here: fixing them means editing `app/models.py`.

## How this is checked
54 schema tests, run against real Postgres + pgvector. Mutation-checked: eight deliberate breakages (old Groq default restored, trigger reverted to INSERT-only, document default flipped to fail-open, an ivfflat index added, role CHECK dropped, an unknown column added, `min_role_required` added to `project_memory`, username made globally unique) were each caught by the intended test.
