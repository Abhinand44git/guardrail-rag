# ADR-0020: Project memory is visible by clearance alone, and defaults to Admin-only

- **Status:** Accepted; amended by ADR-0023 (authors also always see their own entries); its open questions closed by ADR-0025
- **Recorded:** 2026-10-25
- **Related:** ADR-0002, ADR-0004. Migration `0013`. **Supersedes** the Analysis-doc line "Admin sees all entries, Developer/Trainee see only their own" for `GET /project-memory`.

## Context
`project_memory` had no clearance column, so an entry could not be permission-filtered the way documents are. The older docs said Developers and Trainees see only entries they wrote, which is a different rule (and would make a company decision invisible to the colleagues who need it).

## Decision
- `project_memory.min_role_required` (NOT NULL, `CHECK` 1..3, **default 1 = Admin only**), same meaning and same fail-closed default as documents. A person may see an entry if it is in their tenant **and** `min_role_required >= their role number`. Nothing else decides access: not `project_id` (a label, ADR-0004), not the SDLC phase, not who wrote it. One function states this rule: `app/rbac.py::visible_memory_query`.
- Writing: Admin and Developer (a **judgment call**; no source doc named a role). The writer may set a looser level at creation, exactly as ingestion allows.
- Existing rows become Admin-only (the migration's default).

## Consequences
- **Known and deliberate for now:** a Developer who saves a note without choosing a level cannot read it back, and there is **no PATCH or DELETE for memory yet**, so nobody can reclassify or remove an entry. Pinned by `test_a_developers_default_entry_is_admin_only_so_the_developer_cannot_read_it_back`. Options if this proves wrong: Admin-only PATCH/DELETE (mirrors documents), or "authors always see their own" (a second predicate that needs its own tests).
- The Analysis doc line quoted above must be corrected by the owner.

## How this is checked
`tests/test_retrieve_endpoint.py` (clearance, tenant isolation, phase and project narrowing, current-role) and `tests/test_project_memory_endpoints.py`; `test_schema.py::test_project_memory_shape`. Mutation-checked: flipping the comparison, dropping the tenant filter, dropping the model filter, or widening the write roles each fail a test.
