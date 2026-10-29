# ADR-0004: No projects tables: project_id is a label, not a security boundary

- **Status:** Accepted
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Related: ADR-0002, ADR-0003.

## Context
Access in GuardRail is decided by two things only: the caller's tenant and the document's clearance level (`min_role_required`). `project_id` exists on `project_memory` as a bare UUID tag. A `projects` / `project_members` pair would let teams within one tenant be separated from each other, but it needs a second condition in the core RBAC query — the single most consequential query in the system.

## Decision
Do **not** build `projects` or `project_members`. Keep the architecture as is. `project_id` stays a client-supplied label and **must never be described as a security boundary** in any doc, error message or UI text.

## Consequences
- Anyone in a tenant with sufficient clearance can read anything at that clearance, regardless of `project_id`.
- The README must state this limit. (Not yet written.)
- If per-team scoping is ever wanted: add the two tables and a second predicate to `app/rbac.py::visible_document_vectors_query()`, with its own tests.

## How this is checked
`test_table_set_matches_the_documented_schema` (no `projects` / `project_members`) and `test_project_memory_shape` (no foreign key on `project_id`).
