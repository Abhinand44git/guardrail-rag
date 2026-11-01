# ADR-0023: Authors always see their own project-memory entries

- **Status:** Accepted (amends ADR-0020); its open question about editing and deleting entries was closed by ADR-0025
- **Recorded:** 2026-10-25
- **Related:** ADR-0004, ADR-0020, ADR-0021.

## Context
ADR-0020 made memory visible by clearance alone, with a default of 1 (Admin only). Its own listed consequence was that a Developer who saved a note without choosing a level could never read it back. The owner decided that authors should always see what they wrote.

## Decision
A person may see a memory entry if it is in their tenant **and** (their clearance allows it **or** they wrote it):

    tenant = :t  AND  ( min_role_required >= :role  OR  created_by_user_id = :user )

It lives in one place, `app/rbac.py::visible_memory_query`, and `GET /project-memory` and `POST /retrieve` both go through it. The tenant condition sits outside the OR, so authorship never reaches across tenants.

## Consequences
- Reading your own entries needs no Admin and no special level; the default stays Admin-only for everyone else (fail-closed is unchanged).
- Another Developer, a Trainee, or an Admin-only entry written by someone else: no change.
- **Deliberate consequence:** authorship is permanent. If an Admin who wrote Admin-only notes is later demoted, they keep reading the notes they wrote (and only those). That matches "always see their own"; if it is ever unwanted the fix is to require the author's current clearance too.
- Still no PATCH/DELETE for memory: nobody can reclassify or remove an entry after it is saved. Open, owner's decision.
- Documents are unaffected: they carry no author.
- The Analysis-doc line "Developer/Trainee see only their own entries" is now correct only in one respect (they always see their own); everyone else's entries follow clearance.

## How this is checked
`tests/test_project_memory_endpoints.py` (own entry readable at the default level, a different Developer and a Trainee still excluded, authorship never crosses tenants, demoted author keeps only their own, own entries still narrowed by project/phase) and `tests/test_retrieve_endpoint.py` (the same through `/retrieve`, including the model filter). Mutation-checked: removing the author clause, inverting it, making it always true, using AND instead of OR, and the operator-precedence bug `(tenant AND clearance) OR author` each fail a test.
