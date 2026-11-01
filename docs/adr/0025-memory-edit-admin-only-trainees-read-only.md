# ADR-0025: Memory entries can be reclassified or deleted by Admins only; Trainees read but do not write

- **Status:** Accepted
- **Recorded:** 2026-10-28
- **Related:** ADR-0020, ADR-0023, ADR-0021, ADR-0024. Closes the two open questions in ADR-0020 and ADR-0023.

## Context
After ADR-0023 an author can always read their own memory entries, but nobody could change an entry's level or remove one, and it was undecided whether Trainees may write notes at all. The owner asked for both to be settled.

## Decision
- **`PATCH /project-memory/{id}`** (Admin only) changes `min_role_required` and nothing else. **`DELETE /project-memory/{id}`** (Admin only) is a hard delete: no other table references a memory entry, so there is no history to protect (unlike users, which are only deactivated). An id that does not exist and an id in another tenant return the same generic 404.
- **The text of a note is not editable.** Changing it would mean re-embedding it; to change a note, delete it and save a new one.
- **Authors cannot edit or delete their own entries.** This is the rule documents already follow: a role that creates content does not also get to reclassify or remove it (Analysis doc, section 1). The cost is accepted: a Developer who saves a wrong note needs an Admin to remove it.
- **Trainees stay read-only for memory** (they also cannot ingest documents). A saved note is retrieved into other people's AI sessions, so the right to write one is the right to influence what colleagues' models are shown. That is a poisoning risk, and the lowest-trust role should not hold it. Developers and Admins can write.

## Consequences
- Both endpoints use the same single access rule as everything else (`app/rbac.py::visible_memory_query`), with the role read from the database (ADR-0024), so a demoted Admin loses the power on their next request.
- If Trainee writes are wanted later, it is a one-line change to `_WRITE_ROLES`, but it should come with a plan for the poisoning risk (for example notes from Trainees defaulting to Admin-only until an Admin releases them, which is already the default).

## How this is checked
`tests/test_project_memory_endpoints.py`: an Admin reclassifies and visibility changes at once (Trainee sees it, then stops seeing it); only the level changes; Developers and Trainees are refused even for their own entry; no/garbage/operator tokens refused; another tenant's id and a missing id give identical 404 bodies and leave the other tenant's row untouched; invalid levels and malformed ids are 422 with no change; delete removes the row from the list and from `/retrieve`; a second delete is 404; a demoted Admin's old token is refused; a database failure is a safe 500 and the row survives. Mutation-checked: opening either endpoint to Developers or Trainees, dropping the tenant filter, ignoring the requested level, not committing the delete, and loosening the level range each fail a test.
