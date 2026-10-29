# GuardRail RAG — Decision Log (carry into Development)

Compact reference for starting a new chat. Full detail lives in GuardRail_RAG_Analysis.md and GuardRail_RAG_Design.md (both v2.0, corrected) — upload both alongside this file if deeper context is needed.

**Status: Phase 4 complete (Session 6); token blacklist fixed (Session 7); Session 8's seven open items decided; event-loop blocking + multi-worker crash isolation + `/auth/logout` plane split done (Session 9); the three previously-deferred ingest gaps — `DBAPIError` handling, widened PDF exception handling, and the untested surface area (concurrent ingests, exact file-size boundary, large documents, embedding-call failure, missing auth headers) — all fixed (Session 10). **112/112 tests passing** against a real Postgres + pgvector instance, migrations `0001→0012` confirmed clean on a fresh volume (no new migration since Session 7). No known ingestion gaps remain open. Still open: a real embedding-isolation microservice is a named-but-undecided option (Session 9); `POST`/`GET /project-memory` endpoints deferred to Phase 5 by the project owner. Real embedding model, `+cpu` torch wheel, and `docker compose up --build` (now also covering `--workers 2`) remain unverified in this sandbox — see "Session 9"/"Session 10" at the bottom of this log.**

---

## Session 4 — Phase 3.5 built and tested this session

Order: verify Session 3's claims against the actual code first (not just re-read the log), then design and empirically validate the admin-cap trigger extension against a live DB *before* writing it as a migration, then build the endpoints, then re-run the full suite. Same "build, then re-review" discipline as Session 3, applied one step earlier this time — verifying inherited claims before adding to them, not just after.

### Verification pass, before writing any Phase 3.5 code

Unzipped the Phase 3 deliverable and checked its claims against the actual code, per standing instruction to not trust this log blindly:
- Migrations `0001→0006` applied cleanly on a fresh Postgres 16 + pgvector volume (installed directly in-sandbox, same bare-metal approach Session 3 used — Docker Hub unreachable here either).
- `pytest -v` → **46 passed, 0 failed** against that real instance — matches the claimed count exactly.
- Schema inspected directly (`\d users`, `\d tenants`): nullable `llm_*` columns confirmed, no Groq default, `tenants.is_active`/`created_by_operator_id` present, `(tenant_id, username)` uniqueness present. All matched.

**One claim didn't hold up:** this log and `README.md` both stated `app/crypto.py`'s docstrings were fully corrected in migration `0004`'s cleanup (removing the pre-v2.0 "falls back to the default provider key" language). `DecryptionError`'s docstring still had that exact leftover phrase. Not a behavior bug — nothing in the codebase implements that fallback — but a real documentation/code mismatch, found by re-reading the file, not by a failing test. Fixed.

**One shipped-but-undocumented gap, unrelated to the correction:** `README.md` describes `.env` as git-ignored ("see `.gitignore`"), but no `.gitignore` existed anywhere in the shipped tree. Added a minimal one (`.env`, `__pycache__/`, `.pytest_cache/`, etc.).

### Trigger design validated against live Postgres before being committed as a migration

Phase 3.5 introduces two new ways to become an Admin that the original `trg_enforce_max_admins` (Session 2, INSERT-only) doesn't cover: promotion (`PATCH /admin/users/{id}` changing `role`) and reactivation (same endpoint flipping a deactivated Admin's `is_active` back to true) — both `UPDATE`s, not `INSERT`s. Left as-is, both would have silently bypassed the DB-level cap entirely.

Extended to `BEFORE INSERT OR UPDATE OF role, is_active`, counting only `is_active = true` admins. Before writing this as a migration, ran the exact function logic against a scratch table in the live sandbox Postgres instance and confirmed, empirically, all four transitions behave correctly: a fresh admin INSERT at cap is blocked; a promotion at cap is blocked; a promotion after a same-tenant deactivation frees the slot and succeeds; a reactivation that would push over cap is blocked; and an unrelated no-op re-set of `role`/`is_active` on an already-active-admin row does not spuriously raise. Also confirmed the short-circuited `TG_OP = 'UPDATE' AND OLD.role = 1` guard doesn't error on `OLD` during `INSERT` (it doesn't — AND short-circuits before `OLD` is ever dereferenced). Migration `0008`.

A second, smaller gap in the same family: `app/user_provisioning.py::check_admin_cap_or_raise` (the Python-side pre-check, shared by the Operator's admin-creation endpoint and now `POST`/`PATCH /admin/users`) counted *all* admin rows, not just active ones. Harmless while `users.is_active` didn't exist; would have started giving false "cap reached" rejections the moment a deactivation should have freed a slot, since the DB trigger and the app-level pre-check would then disagree about what "an admin" means. Fixed to filter on `is_active = true`, same as the trigger.

### `users.is_active` — the other enforcement half, mirroring the tenant-side pattern from Session 3

New column (migration `0007`), same precedent as `tenants.is_active`: `project_memory.created_by_user_id` / `query_log.user_id` need to keep resolving to a real row, so `DELETE /admin/users` sets this rather than deleting the row.

Not explicitly asked for, but a direct consequence of adding it: without enforcement, deactivating a user would do nothing until their existing JWT expired. Closed the same way Session 3 closed the equivalent tenant-side gap — `app/deps.py::require_user` now also looks up the user row by the token's `sub` claim and checks `is_active` on every request (DB-backed, not a JWT claim, so it takes effect immediately); `POST /auth/login` now rejects a deactivated user's correct password with a distinct, actionable error (distinguishable from wrong-password on purpose — by the time a password verifies, the credential's validity is already established, so naming "deactivated" isn't a new leak). Broke and fixed 4 `test_jwt_planes.py` tests that called `require_user` directly with a token minted for a `uuid.uuid4()` with no backing row (same "mechanical fix, not a design change" situation as Session 3's `require_user`-becomes-async fix) — added a `_user()` fixture helper alongside the existing `_active_tenant()` one, plus 2 new tests for the deactivated/nonexistent-user cases specifically.

### `app/routers/admin.py` built (`POST`/`GET`/`PATCH`/`DELETE /admin/users`)

All four Admin-only, tenant-scoped via the caller's own JWT (no `tenant_id` parameter anywhere for a caller to spoof).

- `POST` — creates Developer/Trainee/Admin. Identical mechanism to `POST /operator/tenants/{id}/admin`: random password (`app/security.py::generate_temp_password`), emailed, never in the response. Admin-cap-checked against `tenant.max_admins` (not a fixed `1` like the Operator endpoint).
- `GET` — paginated (`limit`/`offset`, capped at 200), optional `?role=` filter, tenant-scoped. Added `ORDER BY created_at, id` — the spec says ordering isn't *required*, but `LIMIT`/`OFFSET` with no `ORDER BY` at all is a correctness bug (Postgres doesn't guarantee stable row order across calls without one), so this is the smallest deterministic default, not new "ordering feature" scope.
- `PATCH` — role and/or `is_active`, either/both in one request, same partial-update shape as `PATCH /operator/tenants/{id}`. Runs the same admin-cap pre-check as `POST` on any transition *into* an active Admin (promotion, reactivation, or both at once); the DB trigger (`0008`) is the race-safety net underneath, same "belt and suspenders" relationship as `0002` always had with the Operator endpoint.
- `DELETE` — soft deactivation (`is_active = false`), not a row delete. Kept as the HTTP verb for semantic clarity per the decision below.

**Two judgment calls made this session, not decisions already present in the source docs — flagged explicitly rather than silently assumed, per standing instruction. Both since confirmed with the project owner — see addendum below.**
1. `PATCH /admin/users/{id}`'s `is_active` field accepts both directions, so the same endpoint can also *reactivate* a deactivated user. The Analysis doc describes deactivation but never separately describes a way back; mirrored `PATCH /operator/tenants/{id}`'s existing `is_active: bool | None` shape (which already supports reactivating a tenant) rather than inventing a new one.
2. An Admin cannot target their own user id via `PATCH`/`DELETE /admin/users/{id}` — rejected with a 400 naming the reason. Not stated outright anywhere, but the Analysis doc consistently frames this endpoint as one Admin acting on "another"/"other" user, and without this guard a tenant's sole Admin could deactivate or demote themselves with the tenant then locked out of Admin access entirely — Operators have no path into a tenant's users at all, by design (structural, Session 1). Self-service password change remains a separate, unaffected endpoint. Worth revisiting if a real workflow turns out to need self-targeted role changes specifically through this endpoint.

`app/email.py::send_admin_welcome_email` generalized to `send_new_user_welcome_email(to_email, temp_password, role_name)` so `POST /admin/users` can reuse the identical send-or-rollback mechanism for all three roles, not just Admin. `POST /operator/tenants/{id}/admin`'s call site updated to pass `role_name="Admin"` — one-line touch to Phase 3 code, done for shared-consistency reasons, not unrelated scope creep.

### Tests — 19 new (25 as of the addendum below), end-to-end over ASGI against real Postgres, same standard as `test_operator_endpoints.py`

`tests/test_admin_endpoints.py`: creation (incl. cap enforcement, username collision, non-Admin caller rejected), listing (role filter, pagination, tenant isolation), promotion/reactivation cap enforcement (both directions, empirically exercised through the real HTTP layer, not just the trigger scratch-test above), self-target rejection on both `PATCH` and `DELETE`, generic-404 for both nonexistent and cross-tenant user ids (with a follow-up read proving the cross-tenant row was genuinely untouched, not just hidden in the response), soft-delete-not-hard-delete, cap-freed-by-deactivation, and one full chain test (`test_full_chain_operator_to_admin_to_new_user`) that goes through a real Operator login → real first-Admin creation/email → real `/auth/login` as that Admin → `/admin/users` — proving Phase 3 and Phase 3.5 actually connect end to end, not just that each passes standalone.

First attempt at two of these tests (cap-freed-by-promotion, cap-freed-by-deactivation) used a `max_admins = 1` tenant with two directly-inserted active-Admin rows for setup — the trigger correctly rejected the second insert, which was the test's own setup being wrong, not a bug: with a cap of 1, only one active Admin can exist at a time by definition, so there's no way to have a *second* Admin available to act as caller once the first is deactivated. Fixed by using `max_admins = 2` for those two scenarios instead — same assertions, self-consistent setup.

### Verification — what was proven this session

Fresh volume: dropped and recreated the test database, `alembic upgrade head` applied `0001→0008` cleanly in order, `scripts/seed.py` ran successfully against the new schema (`users.is_active` defaults applied with no migration-order issues). `pytest -v` → **68 passed, 0 failed, 0 errors** — the original 46, plus 2 new in `test_jwt_planes.py`, 1 new in `test_auth_endpoints.py`, and 19 new in `test_admin_endpoints.py`. Manually inspected `\d users` and the seeded rows directly to confirm `is_active` defaults to `true` and the trigger's column list is exactly `role, is_active`.

**Not proven, same caveats as Session 3, unchanged:** the actual `docker compose up --build` path (still verified via bare-metal Postgres instead — Docker Hub unreachable in this sandbox either), and the real-SMTP send path (still only the dev-mode outbox exercised). **Discussed since (not yet done):** running the real `docker compose up --build` path specifically to confirm migration `0008`'s trigger DDL behaves identically on whatever Postgres/pgvector version `docker-compose.yml`'s `supabase/postgres` (or `ankane/pgvector`) image actually ships, versus the bare-metal Ubuntu-apt Postgres 16 + pgvector 0.6.0 this was verified against. Recommended smoke test: `docker compose up --build`, then `alembic upgrade head` and `pytest -q` inside the `api` container.

### Addendum (same chat, after this session's initial writeup) — both judgment calls confirmed, reactivation gains an email-confirmation requirement

Discussed both judgment calls above with the project owner. Both confirmed as-is — no changes to the self-target guard. Reactivation gained one addition as a result of that discussion:

**`PATCH /admin/users/{id}` reactivation (`is_active: false → true`) now also requires `confirm_email`, matching the target user's stored email exactly**, checked before the admin-cap check (being sure it's the right person matters before checking whether there's cap room for them). Not a security boundary — the acting Admin already sees every user's email via `GET /admin/users` — it's a deliberate speed bump against restoring access for the wrong row out of a list of deactivated users, the concrete failure mode the project owner had in mind (an Admin managing a longer user list fat-fingering the wrong deactivated row). Two other approaches were discussed and set aside, worth recording why:
- **A fresh company/employee id for the rehired person** — rejected. The entire reason `DELETE` is a soft deactivation rather than a hard delete is so a rehired person's `project_memory`/`query_log` history stays attached to the same row when they come back; issuing a new id on reactivation would orphan that history under the old, now-permanently-dead id, undoing the reason soft-deactivation exists at all.
- **Step-up password re-entry** (the "sudo window" pattern GitHub/AWS use — re-authenticate once, sensitive actions skip the re-prompt for a following window) — set aside for now as heavier than this specific risk warrants (it defends against token theft, not against picking the wrong row, which is the actual concern here), but flagged as worth revisiting if token theft becomes an explicit threat model this project wants to demonstrate defenses against.

`email` was chosen over `username` as the confirmation field because it's already globally unique (not just per-tenant) and is what `/auth/login` already keys on — using a second, different identifier for confirmation than the one already treated as canonical elsewhere would have been an inconsistency, not a new decision. Comparison is exact-match after trimming whitespace only, no case-folding — consistent with the rest of the codebase, where no email lookup anywhere (including `/auth/login`) does case-insensitive comparison.

Deactivating needs no confirmation (same as `DELETE`, which was never in question) — only the false→true transition triggers it; a role-only `PATCH` that never touches `is_active`, and a no-op `is_active: true` sent to an already-active user, are both correctly unaffected (each has its own test).

6 new tests in `tests/test_admin_endpoints.py` (25 total in that file, 74 overall): correct-email succeeds, wrong-email rejected (with a follow-up read proving the row was genuinely untouched), missing-email rejected, and three "not required" tests (deactivation, plain role change, no-op already-active). One existing test (`test_patch_reactivating_admin_blocked_by_cap`) needed a matching `confirm_email` added to its request so it continued to actually exercise the cap check it's named for, rather than failing earlier at the new identity check instead — same status code either way, so this would have silently kept "passing" for the wrong reason if not caught. Full suite re-run after: **74 passed, 0 failed, 0 errors.**

### Still open, carried forward

- Exact mechanism for the Admin notification when a company key fails at query time (email vs. in-app flag vs. status-on-login) — unchanged from Session 3, not touched this session; still Phase 5 scope.
- `GuardRail_RAG_Analysis.md` — not yet edited to reflect either judgment call or the `confirm_email` addition (unlike Session 3, which did physically rewrite it for its own decisions). Now genuinely due, since both calls are confirmed rather than open questions; produce as its own deliverable when requested.
- The `docker compose up --build` smoke test noted above, specifically to de-risk migration `0008` against whatever Postgres/pgvector version the compose file's image actually pulls, before stacking Phase 4/5/6 on top.

### Open: embedding hosting conflict (found in Session 4/5 — **RESOLVED and propagated in Session 6**, see "Session 6" at the bottom; original text kept below as history)

Three-way inconsistency, found while answering a question about it, not by inspection of code (there's no ingestion code yet — Phase 4 hasn't been built):
- `GuardRail_RAG.md` and `GuardRail_RAG_Design.md` both say embeddings go through the **HuggingFace *Inference API*** (an external network call) — "Embeddings: BAAI/bge-small-en-v1.5 (HuggingFace Inference API, 384-dim)".
- This log (the "Confirmed stack" section, unchanged since it was written) says the opposite: **"self-hosted `BAAI/bge-small-en-v1.5` via `sentence-transformers` inside the `api` container... embeddings were never the issue"** — no external call at all.
- The shipped scaffolding agrees with the *external-API* reading, not this log's: `app/config.py` has `hf_api_token` (a self-hosted local model needs no API token), `.env.example` labels it `# Embedding provider (HuggingFace Inference API)`, and `requirements.txt` has no `sentence-transformers`/`torch`/`huggingface_hub` — there is currently no code path capable of running the model locally at all.

**Decided this chat: self-hosted.** Sending every ingested chunk (the entire private corpus, not just what a query's RBAC/similarity filter surfaces) and every query's text to a third party's servers in plaintext sits badly against this project's own stated "no unnecessary vendor exposure" framing, and is arguably a larger exposure than the generation-model question the original correction was about — it isn't gated by any Admin configuration step the way generation is, and touches 100% of ingested content, not just retrieved chunks. `bge-small-en-v1.5` is small (~130MB, MIT-licensed) and CPU-viable, so self-hosting isn't a heavy trade either.

**Not yet done:** which model, specifically — `bge-small-en-v1.5` is the name currently written down everywhere, but was never confirmed as the right choice *under the self-hosted constraint specifically* (its Inference-API framing may have been part of why it was picked originally). Model selection is being handled as its own separate chat (deliberately not mixed into Phase 3.5's build-and-review work) — bring back whichever model that chat lands on. Once decided: `GuardRail_RAG.md` and `GuardRail_RAG_Design.md` both still need editing to drop "HuggingFace Inference API" language (same class of doc-drift as the Groq fix — decided in this log, never propagated to the other docs), and the scaffolding (`config.py`, `.env.example`, `requirements.txt`) needs the self-hosting dependencies added and the API-token framing removed. None of this blocks Phase 4 from starting on the ingestion/chunking side, just the embedding-call piece specifically.

---

Everything below happened in one chat. Order matters: the pre-Phase-3 fix came first, then the endpoints, then a deliberate post-build review that found two more gaps — flagging that structure because it's the working pattern worth repeating in future phases (build, then re-review before calling it done).

### Pre-Phase-3 fix: the Groq correction from the previous session was doc-only

The "Major correction" section right below this one describes a **previous** session's fix — but that fix only ever touched the design docs. The shipped code was never updated to match, and this was found by inspecting it, not by trusting the log. Specifically:

- `alembic/versions/0001_initial_schema.py` and `app/models.py`: `tenants.llm_provider`/`llm_model_name` were still `NOT NULL` with a `'groq'`/`'llama-3.1-8b-instant'` server default.
- `app/config.py`: still had app-wide `default_llm_provider`/`default_groq_api_key`/`default_groq_model` settings.
- `app/crypto.py`: docstrings described clearing a tenant's key as "falling back to the default provider key" — a fallback that doesn't exist in the corrected design.

**Fixed via migration `0004`** (drops the `NOT NULL`/default on both columns) plus corresponding changes to `models.py`, `config.py`, `.env.example`, and `crypto.py`'s docstrings. Proven against a real Postgres instance, not just reasoned about: a tenant inserted with no model specified now returns `NULL`/`NULL`/`NULL`, not the old Groq default. `0001` itself was left untouched (never edit an applied migration) — `0004` is the correction layered on top, which is expected and correct Alembic history, not a leftover bug.

### Phase 3 endpoints built (`app/routers/operator.py`)

- `POST /operator/login` — moved from `/auth/operator/login` per the Design doc's corrected naming. **Old path removed, not aliased** — a request to it now 404s (regression-tested).
- `POST /operator/tenants` — creates a tenant with no AI model configured (relies on the `0004` fix above). Response deliberately excludes `llm_*` fields — keeps "tenant lifecycle" (Operator concern) separate from "tenant AI config" (Admin concern) at the read level too, not just the write-endpoint level.
- `POST /operator/tenants/{id}/admin` — creates an Admin with a cryptographically random password (`secrets`, not `random`). Password is **never** in the API response — delivered only via `app/email.py`. All-or-nothing: DB write and email send succeed together or the whole thing rolls back (an Admin account nobody can log into is a support burden, not a partial success).
- `PATCH /operator/tenants/{id}` — `is_active`/`max_admins` only, does not touch AI config. Has a dedicated test proving the "grandfather" behavior: lowering `max_admins` doesn't remove/demote existing Admins, only blocks new ones (the DB trigger only fires on INSERT).

**New schema** (migration `0005`): `tenants.is_active` and `tenants.created_by_operator_id` — both specified in the Design doc's schema but never actually migrated in during Phase 1/2, since no operator-plane code existed yet to use them.

**Email** (`app/email.py`, new): minimal, send-only, subject+body, exactly the decision log's existing Phase 3 item #4 — not the full reset flow. Auto-falls-back to an in-memory dev outbox when `SMTP_HOST` is unset (the sandbox this was built in has no SMTP egress at all). Real `smtplib`/STARTTLS code exists for when `SMTP_HOST` etc. are set on Railway/Render, but **that path has not been verified against a live mail server** — only the dev-mode path has been exercised, via the test suite.

### Contract decisions locked for Phase 3.5/5 (not built yet, just decided)

These came up while reviewing Phase 3's scope boundary, ahead of when they're actually needed:

- Request body for setting a model key uses `llm_api_key` (raw, incoming) — `llm_api_key_ref` is reserved for the encrypted/stored form only. Avoids one field name meaning two different things (plaintext in, ciphertext-reference at rest) depending on which moment you're looking at.
- `PATCH /user/llm-config` = set only (validate-then-save, no branching). New `DELETE /user/llm-config` = clear only. Split from a single dual-behavior PATCH specifically to keep the security-sensitive write path (handles an unencrypted key in flight) to one job per endpoint, no internal branch on payload shape.
- `/operator/login` (not `/auth/operator/login`) confirmed as the permanent name — see endpoints above, this is now live, not just decided.

### Post-build review — two more gaps found and fixed, same session

After the endpoints were built and tested, a deliberate second pass (prompted by "anything missed or messed") found two real gaps, both fixed and tested before this session ended:

1. **`is_active` had a write side but no read side.** `PATCH /operator/tenants/{id}` could deactivate a tenant, but nothing in the app actually checked `is_active` anywhere — a deactivated tenant's users could still authenticate normally. Fixed: `app/deps.py::require_user` now checks `tenant.is_active` against the DB on every request. Deliberately **not** encoded as a JWT claim — a claim would mean deactivation only takes effect once a user's token naturally expires/refreshes, defeating the point of an Operator being able to cut off access immediately. This made `require_user` async and DB-backed, which broke five Phase 2 tests that called it directly as a sync, DB-free function — all five updated to `await` it against a real committed tenant row (mechanical fix, not a design change).
2. **`users.username` had no uniqueness constraint at all** — same shape of bug as the already-fixed `users.email` gap, found by inspection this time, not by a failing test. Fixed via migration `0006`, scoped to `(tenant_id, username)`, deliberately **not** global — unlike `email` (which `/auth/login` resolves by itself, across the whole table, so it has to be globally unique), nothing looks a user up by `username` alone. Per-tenant scoping means two unrelated companies can both have an admin literally named `admin` with zero collision — no forced naming convention (e.g. full surnames) needed to avoid cross-tenant collisions, which was the concern that prompted this fix. Regression-tested both directions: rejected within one tenant, allowed across two different tenants.

Fixing #2 also surfaced a latent bug in the Phase 3 admin-creation endpoint's error handling: the flush-time exception handler assumed any DB-level failure at that point meant "admin cap exceeded" and said so — but a username race could now also land there, and the response would have named the wrong cause. Added an explicit pre-check for username collisions (parallel to the existing admin-cap pre-check) with its own precise error, and made the flush-time fallback message generic rather than presuming a specific cause, since it can now legitimately fire for either race.

### Decided this session: who creates Admins beyond the first

Genuinely unresolved across the source docs before this session — `/operator/tenants/{id}/admin` was built reusable up to `max_admins`, but Phase 3.5's `POST /admin/users` might also have been meant to handle it. **Resolved:** after the first Admin, subsequent Admins are created by an existing Admin (presumably via Phase 3.5's `/admin/users`), not by the Operator calling the same endpoint repeatedly.

**This creates a real conflict with the Analysis doc as currently written**, not yet reconciled: Analysis's API surface section scopes `POST /admin/users` to *"create Developer/Trainee accounts"* explicitly — this decision means that scope needs to widen to include Admin creation once Phase 3.5 is actually built. Flagging this here rather than silently editing the Analysis doc, since it wasn't in front of me to edit and the decision was made in this log's context, not that one's.

**Still genuinely open, not decided this session:** whether `/operator/tenants/{id}/admin` should be restricted to first-admin-only *right now* (matching the decision above, but leaving any tenant with `max_admins > 1` unable to get a second Admin until Phase 3.5 ships — a real temporary capability gap) or left reusable-up-to-cap as an interim stopgap until Phase 3.5 exists. Asked, not yet answered as of end of session.

### Verification — what was and wasn't proven this session

Proven against real infrastructure, not mocked: Postgres 16 + `postgresql-16-pgvector` installed directly (not via Docker — see below), all six migrations applied cleanly on a fresh volume in order, seed script run successfully afterward, full 46-test suite passing against that real instance, including the grandfather-behavior test and the emailed-password-actually-works test (verified against `app/email.py`'s dev outbox, then confirmed the captured plaintext password verifies against the stored bcrypt hash).

**Not proven, worth knowing before deploying:** the actual `docker compose up --build` path the README tells a new developer to use — Docker Hub isn't reachable from the sandbox this was built in, so everything was verified against a bare-metal Postgres install instead. The code should behave identically either way, but that's "should," not "proven," same distinction as the real-SMTP path noted above.

### Phase 3 — verified this session (same format as the Phase 2 entry below)

- Fresh volume, migrations `0001→0006` applied cleanly, in order.
- Seed script ran successfully on top of the new schema (`is_active`, `created_by_operator_id`, per-tenant username uniqueness all present, nothing broke).
- `pytest -v` → **46 passed, 0 failed, 0 errors** against real Postgres + pgvector — `test_auth_endpoints.py`, `test_crypto.py`, `test_jwt_planes.py`, `test_operator_endpoints.py` (new, 14 tests), `test_rbac.py` all green.
- Manually inserted a tenant via the real endpoint and inspected the row directly (`\d tenants`, then a live `INSERT`/`SELECT`) — confirmed `llm_provider`/`llm_model_name`/`llm_api_key_ref` are `NULL`, not the old Groq default.

---

## ⚠️ Major correction from an earlier session — read this before anything else

(This section predates Session 3 above — it's the session that corrected the *design docs* to remove GuardRail's owned-default-model concept. Session 3 is what found and fixed the *code* that still didn't match this correction. Keeping this section as-is below since the architecture decision itself is still accurate and unchanged.)

**Original core idea, restated correctly:** GuardRail is a security/RAG shield that sits between a company's private documents and whatever AI model the company already uses — it is **not** an AI provider itself. Companies already pay for or self-run a model; GuardRail's entire job is making it safe to point that model at private data, by only ever handing it small, permission-filtered chunks, never full documents.

**What this corrects:** two earlier revisions of this log each had GuardRail defaulting to an AI model it owned — first Groq (`llama-3.1-8b-instant`), then (in a prior session) a reversal to self-hosted IBM Granite via Ollama, justified at the time as closing a "third-party sees plaintext on every query" gap. **Both of those were solving a problem that shouldn't have existed.** If GuardRail never provides a default model in the first place — every tenant brings their own — that exposure question is moot from the start; there's no GuardRail-chosen vendor to leak to.

**Everything from the self-hosted-Granite thread is now superseded and removed from active scope:**
- The Ollama-vs-`llama-cpp-python` networking-mechanism decision — moot, there is no model to host.
- The `ibm/granite4:1b-h` model pull and tag-naming notes — no longer relevant.
- The `tenants.llm_provider` default-value question (`'groq'` vs `'local'`) — resolved differently: **no default at all**. The column starts null; a tenant is unusable for `/query` until an Admin explicitly configures it.
- The self-hosted-generation latency risk (p95 < 3s on CPU-only hardware) — no longer GuardRail's architectural problem; latency is now a per-tenant variable depending on whatever model that tenant/user has configured.

## New architecture, confirmed in the design-doc-correction session

- **Admin** sets the tenant's company-default AI model via a new endpoint, `PATCH /admin/tenant-config`. Required before that tenant's `/query` becomes usable — there is no fallback.
- **Every tenant role except Operator** (Admin, Developer, Trainee — all three, same mechanism) can personally override with their own model/key via `PATCH /user/llm-config`, or by switching models directly in a `/query` request.
- **Precedence:** a user's personal config, if set, always wins over the tenant default.
- **Switching persists:** picking a different model while querying isn't a one-time thing — it's saved as that user's new personal default from then on. The `/query` response says so explicitly (`note` field) so this isn't a silent state change.
- **Where personal config lives:** a new `user_llm_config` table (one row per user who's opted in), not columns on `users` — kept separate to isolate a security-sensitive column (API key reference) from a table queried constantly for unrelated reasons, and to leave room for richer config later without a migration.
- **Key validation:** any write of a model key (tenant-level or personal) is tested against the provider immediately. Failure blocks the *entire* write — no half-saved provider/model name without a working key. Same rule, no role-based exception.
- **Query-time failure handling:** if the tenant's company-default key fails, the Admin is notified immediately (it affects everyone without a personal override). If a user's own personal key fails, that user alone gets a safe, explicit error — no Admin notification needed for a personal-key issue.
- **RBAC is untouched by any of this.** Which document chunks get retrieved still depends purely on role/clearance; model choice and document access are separate axes, confirmed not to interact.

## Confirmed stack
FastAPI · PostgreSQL + pgvector (hosting: Supabase near-term — see "Database hosting" below) · JWT + bcrypt · Docker Compose (2 services: `api`, `db` — no model-serving container, see correction above) · self-hosted `BAAI/bge-small-en-v1.5` via `sentence-transformers` inside the `api` container for **embeddings only** (this part is unaffected by the generation-model correction — embeddings were never the issue) · deploy on Railway/Render.

## Chat/history visibility and admin oversight (carried forward, unaffected by the design-doc correction or Session 3)

- **Query history** (`GET /query/history`) — in scope. Needs `query_log`: `id`, `tenant_id`, `user_id`, `project_id`, `query_text`, `created_at`.
- **`project_memory.created_by_user_id`** — records who saved each entry.
- **Visibility rule:** Admin sees everything; Developer/Trainee see only their own records — a second, separate access axis from the document-sensitivity RBAC filter in `app/rbac.py`. Kept structurally distinct from `visible_document_vectors_query()`.
- **Admin audit trail:** stays deferred as a full feature. Lightweight derived indicator only — `MAX(created_at)` computed at read time across `project_memory` + `query_log`, no new table, no write-time triggers, "modified only, not opened."
- No `projects` table exists; `project_id` remains a bare UUID tag. Not being added now.

## Resolved in the design-doc-correction session — previously open gaps

1. **Admin user management phase** — assigned to **Phase 3.5**, immediately after Phase 3. It cannot run earlier: Phase 3 is where the tenant's first Admin is created, and there's no Admin to manage users before that exists.
2. **Document deletion / sensitivity-update authority** — **Admin-only.** Deliberately not extended to whoever ingested the document, to avoid a role that creates content also automatically gaining the power to delete or reclassify it.
3. **Route naming drift** — the shipped code has `/auth/operator/login`; the corrected Design doc uses `/operator/login` going forward. Treated as a rename to apply in new/updated code, not left as permanent drift. **Done in Session 3**: `/operator/login` is live, `/auth/operator/login` is removed (not aliased) and regression-tested to confirm it 404s.
4. **First Admin's password** — random password generated and **emailed**. Built as a minimal, send-only implementation in Phase 3 (subject + body, nothing more) — explicitly *not* the full password-reset flow, which stays a Phase 6 build. This does pull a small piece of Phase 6 forward; documented here so it isn't mistaken later for the finished feature. **Done in Session 3**: `app/email.py`, live in `POST /operator/tenants/{id}/admin`. Dev-mode (in-memory outbox) verified via tests; real SMTP send path exists but is unverified against a live mail server (no SMTP egress in the build sandbox).
5. **`PATCH /operator/tenants/{id}` scope** — stays narrow: `is_active` / `max_admins` only. AI model config is deliberately handled by a separate, Admin-owned endpoint (`PATCH /admin/tenant-config`), not folded into the Operator's tenant-lifecycle endpoint — keeps "tenant lifecycle" (Operator concern) and "tenant AI config" (Admin concern) as separate responsibilities. **Done in Session 3**, including the "grandfather" behavior test (lowering `max_admins` doesn't remove/demote existing Admins).

## Database hosting — still open, reasoning sharpened this session

Three options, not yet built:
1. **Supabase** — free tier, least ops work, free RLS/Auth extras. Holds the encryption keys by default (SOC 2 Type 2 compliant, but not the same as "Supabase literally cannot read the data"). **Cheapest right now — effectively $0/month at this project's scale.**
2. **AWS RDS for Postgres with customer-managed KMS keys** — you, not Amazon, control who can decrypt; a real, meaningful reduction in third-party data custody. **Not free** — no permanent free tier (only a 12-month trial credit), real hourly + KMS + storage costs apply even for a small project.
3. **Fully self-hosted Postgres** — removes third-party custody entirely; free in dollar terms, but the cost shows up as your own time (backups, patching, uptime) — **and this isn't something the project owner currently knows how to operate safely.**

**Decision:** stay on **Supabase for Phase 7** (matches original Design, matches current skill level, matches the project's timeline). Customer-managed key custody (AWS RDS or self-hosted) is documented as an **intentional, stated future improvement** — the tradeoff is understood, it's just sequenced after the operational skill to run it safely exists. This is a stronger, more honest portfolio story than picking the "ideal" option without being able to explain how you'd operate it.

## Phase-3-specific decisions that remain genuinely open

From before Session 3 (unaffected by it):
1. Whether validation-on-save (§ above) should ever allow a *partial* save (e.g., save provider/model name, flag key as unverified) — currently decided as strict all-or-nothing; revisit only if that turns out to be too rigid in practice.
2. Exact mechanism for the Admin notification when a company key fails at query time (email, in-app flag, a status field surfaced on next login) — the *rule* ("Admin gets told immediately") is settled; the *delivery mechanism* is not.

New from Session 3, resolved later in the same session:
3. ~~Whether to restrict `POST /operator/tenants/{id}/admin` to first-admin-only now.~~ **RESOLVED: yes, locked.** `POST /operator/tenants/{id}/admin` now only ever creates a tenant's first Admin — any existing Admin row for that tenant rejects the call with a safe 400 (`"This tenant already has an Admin..."`), regardless of `max_admins`. Accepted consequence: any tenant with `max_admins > 1` has no way to get a second Admin until Phase 3.5 (`POST /admin/users`) ships. Code + tests updated; 46/46 still passing (2 tests rewritten to simulate a second Admin via direct DB insert instead of a second call to this now-restricted endpoint, since that's genuinely how it'll happen once Phase 3.5 exists).
4. **Analysis doc's `POST /admin/users` scope — RESOLVED, doc updated.** Confirmed: an Admin can create Developer, Trainee, *or another Admin*, using the identical pattern as `POST /operator/tenants/{id}/admin` — random password, emailed, never in the response, user can change it later. `GuardRail_RAG_Analysis.md` has been rewritten accordingly (role table, `/admin/users` endpoint description, decision-log table) — this is a genuine edit to that document, not just a note in this log.

**Resolved:** "user can change it later" turned out to point at a real missing capability — no self-service password-change endpoint existed anywhere in the documented API surface (`/auth/login`/`/logout`/`/refresh` are the only Auth routes; Phase 6's "full password reset" is the *forgot*-password flow, a different feature). Confirmed as its own separate, user-initiated (not Admin-initiated) capability — doesn't block or depend on Phase 3.5, reasonable to build as its own small piece or bundled into Phase 6.

## Roles and identity (unchanged from the design-doc-correction session, updated by Session 3)
- Two identity planes: `operators` (global, no `tenant_id` column anywhere — structural isolation) and `users` (tenant-scoped).
- One user = one tenant = one role. No multi-tenant membership.
- Fixed roles only: Admin (1) / Developer (2) / Trainee (3). No custom role creation. [Revisit if you want the "designed for extensibility" resume line — deferred, not forgotten.]
- Admin count capped via `tenants.max_admins` (default 1, max 5) — enforced by DB trigger `trg_enforce_max_admins`. Lowering the cap grandfathers existing Admins (trigger only fires on INSERT) — proven with a dedicated test in Session 3, not just asserted.
- **Updated by Session 3:** Operators create tenants and (per the endpoint as currently built) can create any Admin up to `max_admins`, not just the first — see open item #3 above, this may get restricted to first-admin-only. Beyond whatever the Operator creates, subsequent Admins are meant to come from an existing Admin, not the Operator (decided this session, not yet built — Phase 3.5).
- Operators cannot reach `/query`, `/ingest`, any document data, or any AI model configuration — enforced structurally via JWT claims, confirmed live in a prior session by decoding a real operator token (no `tenant_id`, no `role`). Session 3 added one more structural check on the *user* side of this boundary: `require_user` now also verifies the user's tenant is `is_active` against the DB on every request, not just that the JWT plane/shape is correct.

## The one query the security model rests on (unchanged)
```sql
WHERE dv.tenant_id = :tenant_id AND dm.min_role_required >= :user_role_number
```
Lower role number = higher clearance. Centralized in `app/rbac.py::visible_document_vectors_query()`. Covered by `tests/test_rbac.py` — 5 tests, previously confirmed passing against a real DB (admin-sees-all, developer-scoped, trainee-scoped, direction-not-inverted, cross-tenant isolation). **Confirmed in the design-doc-correction session, conceptually, and untouched by Session 3:** this query and its test coverage are entirely unaffected by the generation-model correction or by Phase 3's operator endpoints — model choice, tenant activation, and document RBAC are three separate concerns, designed not to interact, and Session 3 didn't touch `app/rbac.py` at all.

## Multi-model support (corrected — the ownership question, not just the mechanism)
- **Generation model:** no GuardRail-owned default. Company-default is Admin-set and required; personal overrides available to all three tenant roles. See the correction section at the top of this log for full history.
- **Embedding model:** unaffected by this correction. `document_vectors.embedding` widened to `VECTOR(1536)`, zero-padded for smaller models, `embedding_dims` tracks true dimension. Every retrieval query filters on `embedding_model` explicitly.

## Secrets, concretely (extended, not changed in kind)
Two tiers. (1) Static app secrets (DB password, JWT key) → hosting platform env vars, never in code/Git. (2) Dynamic, per-row BYO API keys — now covering both `tenants.llm_api_key_ref` *and* `user_llm_config.llm_api_key_ref` → Fernet-encrypted before writing; one `FERNET_MASTER_KEY` env var; decrypt only in memory, only for the call duration, never logged. The mechanism doesn't change with more rows using it — just more places it applies.
Key rotation: still explicitly out of scope (no supporting incident-response infra exists yet).

**Session 3 addition, different in kind from the above:** `SMTP_HOST`/`SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`SMTP_FROM_EMAIL` — plain env vars for the Admin-welcome email, not Fernet-encrypted (this is the app's own outbound mail credential, not a per-tenant BYO key). All optional; `SMTP_HOST` empty (the default) means dev-mode, no network send at all. Not yet set against a real provider — see "Not proven" note above.

## Operator can adjust `max_admins`
`PATCH /operator/tenants/{id}`. Lowering the cap doesn't remove/demote anyone — trigger only fires on INSERT, blocks new admin creation until count drops back under naturally. **Confirmed in Session 3**, not just designed: dedicated test creates two Admins under a cap of 2, lowers the cap to 1, proves both original Admins are untouched, and proves a third Admin is rejected.

## Environment gotchas from prior sessions' Phase 2 re-verification (unchanged, still accurate)

1. `scripts/seed.py` does not run Alembic migrations itself — depends on `alembic upgrade head` having already run. On a fresh Docker volume, seeding before migrating fails with `asyncpg.exceptions.UndefinedObjectError: type "vector" does not exist`. Always run migrations before seeding on a fresh volume.
2. A local dev machine can have a second, independent PostgreSQL installation bound to port 5432 outside Docker, silently intercepting host-side connections and producing a misleading `InvalidPasswordError`. Check `netstat -ano | findstr ":5432"` before assuming a credentials problem.
3. Stale Docker volumes carry stale Fernet-encrypted data under whatever `FERNET_MASTER_KEY` was active when written — regenerating `.env` secrets without `docker compose down -v` leaves old data undecryptable. **Doesn't yet apply to `user_llm_config`** — that table still doesn't exist (Phase 3.5/4 still, unaffected by Session 3); Session 3 only touched `tenants` (dropped the Groq default, added `is_active`/`created_by_operator_id`) and `users` (added the per-tenant username uniqueness constraint) — neither of those changes is Fernet-related.

## Phase 2 — independently re-verified in a prior session (unaffected by the design-doc correction or Session 3)
- Fresh volume, migrations `0001→0003` applied cleanly, seed script populated 1 operator + 2 tenants + 3 users/tenant + 3 docs/tenant.
- `docker compose ps` healthy, no FATAL noise in `db` logs.
- `/health` → `200 {"status":"ok"}`.
- `.local`-domain login succeeds end-to-end.
- Decoded a real user JWT and a real operator JWT live — plane separation confirmed exactly as designed.
- `pytest -v` → 29 passed, 0 failed, 0 errors, against real Postgres + pgvector — `test_crypto.py`, `test_jwt_planes.py`, `test_rbac.py`, `test_auth_endpoints.py` all green.

## Development phases (one chat per phase, paste this log at the start of each)
1. Foundation — **complete**, re-verified in a prior session.
2. Security core (High effort) — **complete**, re-verified in a prior session (29/29 tests passing against real infra).
3. Operator endpoints — `/operator/login`, `/operator/tenants`, admin creation (+ email-based password), `max_admins` PATCH + grandfather test. **Complete, Session 3** — 46/46 tests passing against real infra (see Session 3 section above for full detail, including two gaps found and fixed after the initial build: tenant-deactivation enforcement, per-tenant username uniqueness). Not yet proven through the actual Docker Compose path or a live SMTP server — both were verified via workarounds (bare-metal Postgres, dev-mode email outbox) since the build sandbox couldn't reach either.
3.5. Admin user management — `GET/POST/PATCH/DELETE /admin/users`. **Complete, Session 4** — 68/68 tests passing against real infra (see Session 4 section above for full detail). `POST` fields: `username`, `email`, `role` (Admin picks Developer/Trainee/Admin) — random password generated + emailed with username, same pattern as Phase 3, no password in response. `GET` paginates and filters by role, ordered by `created_at, id` for deterministic pagination. `PATCH` changes role and/or `is_active` (both directions — reactivation included, a judgment call, see Session 4), one endpoint, partial-update (promoting to Admin, or reactivating an Admin, goes through the same cap check as `POST`). `DELETE` is a soft deactivation (`users.is_active`, migration `0007`, mirrors `tenants.is_active`) — not a hard row delete. Deactivating an Admin frees their `max_admins` slot — `trg_enforce_max_admins` updated (migration `0008`) to count only active Admins and to fire on promotion/reactivation, not just creation. An Admin cannot target their own user id via `PATCH`/`DELETE` (judgment call, see Session 4). Self-service password change (user-initiated, after login) remains separate, unphased scope — still not built, doesn't block Phase 4.
4. Ingestion — upload, chunking, embedding call, zero-pad to VECTOR(1536), **plus new delete/reclassify endpoints (Admin-only)**. **Complete, Session 6** — 91/91 tests passing against real infra (see Session 6 section at the bottom of this log). Real embedding model not exercised (no huggingface.co access in the build sandbox) — tests use a fake embedding backend via dependency injection.
5. Query/retrieval + generation — RBAC-filtered vector search, context budget, model-resolution chain (`user_llm_config` → `tenants` → error-if-neither), no GuardRail-owned model call of any kind.
6. Hardening — rate limiting, generic errors, **full** password reset (Phase 3's email send gets fortified here), CORS/HTTPS check.
7. Deploy + polish — Railway/Render, Supabase, secrets set (now including `user_llm_config` encryption), README documenting the AWS RDS/self-hosted future-improvement path.

Status: **Phase 4 complete and tested (Session 6) — 91/91 passing against real Postgres + pgvector, migrations `0001→0011` clean.** Phase 3.5 (`/admin/users`) unchanged from Session 4. Still not built: `PATCH /admin/tenant-config`, `PATCH /user/llm-config`, `user_llm_config` table, `/query`, `POST`/`GET /project-memory` endpoints (table exists), `token_blacklist` table, `/auth/logout` — see Session 6 section for which are Phase 5 scope and which are unaddressed earlier-phase gaps.

## Known fixes from Phase 1 & 2 (unchanged from prior revision, still accurate)
- `LoginRequest.email` uses plain `str`, not `EmailStr` (fixed in Phase 2, regression-tested).
- docker-compose `db` healthcheck targets the correct database name (fixed in Phase 2).
- `bcrypt==4.0.1` pinned to silence passlib's spurious version-read log noise.
- `users.email` unique index added via migration `0003` (fixed in Phase 2, regression-tested).

## Known fixes from Session 3
- **Architecture drift, not just a naming issue**: the design-doc correction (GuardRail owns no generation model) was never applied to the shipped schema/code. `tenants.llm_provider`/`llm_model_name` were `NOT NULL` with a `'groq'`/`'llama-3.1-8b-instant'` default at the DB level; `app/config.py` had app-wide `default_llm_provider`/`default_groq_*` settings; `app/crypto.py` described a fallback key that doesn't exist. Fixed via migration `0004` + `models.py`/`config.py`/`.env.example`/`crypto.py` changes. Proven against a real Postgres instance: a tenant created with no model specified gets `NULL`/`NULL`/`NULL`.
- `/auth/operator/login` renamed to `/operator/login`, removed (not aliased). Regression-tested.
- `tenants.is_active`/`created_by_operator_id` — specified in the Design doc, never migrated in during Phase 1/2. Added via migration `0005`.
- `is_active` had a write side (`PATCH /operator/tenants/{id}`) but no enforcement anywhere — a deactivated tenant's users could still authenticate. Fixed: `app/deps.py::require_user` now checks `tenant.is_active` against the DB on every request (not a JWT claim, so it takes effect immediately). Broke and then fixed 5 Phase 2 tests that called `require_user` directly as a sync, DB-free function.
- `users.username` had no uniqueness constraint at all — same shape of bug as the `users.email` fix above, found by inspection. Fixed via migration `0006`, scoped to `(tenant_id, username)` — deliberately not global, since nothing looks a user up by username alone the way login does by email.

## Phase 2 artifacts (Security core)
Unchanged: `app/crypto.py`, `app/rbac.py`, `app/config.py` (Fernet startup validation), `alembic/versions/0003_users_email_unique.py`, `tests/` (29 tests total), `requirements-dev.txt`. Secret wiring documented in README.md "Secrets, concretely."

## Phase 3 artifacts (Operator endpoints, Session 3)
New: `app/routers/operator.py`, `app/email.py`, `app/user_provisioning.py` (shared admin-cap/username pre-checks, extracted for Phase 3.5 to reuse rather than duplicate), `alembic/versions/0004_remove_tenant_llm_default.py`, `alembic/versions/0005_tenant_lifecycle_columns.py`, `alembic/versions/0006_users_username_unique_per_tenant.py`, `tests/test_operator_endpoints.py` (14 tests).
Modified: `app/models.py`, `app/config.py`, `app/crypto.py`, `app/security.py` (added `generate_temp_password`), `app/schemas.py`, `app/routers/auth.py` (operator login removed), `app/main.py`, `app/deps.py` (`require_user` now async + DB-backed), `.env.example`, `tests/test_crypto.py`, `tests/test_auth_endpoints.py`, `tests/test_jwt_planes.py` (5 tests updated for the async `require_user` signature, 3 new tests added).

**Not built yet (still accurate, unaffected by Session 3):** no HTTP endpoint sets `llm_api_key_ref` on either `tenants` or the new `user_llm_config` yet (Phase 3.5/5), no `/query` endpoint yet (Phase 5), no model-validation-on-save logic yet, no `user_llm_config` table migration yet (needs to exist before Phase 5 can resolve models). Session 3 deliberately stayed inside the Operator-endpoint scope boundary and did not build any of these, even though the `PATCH /admin/tenant-config`/`PATCH /user/llm-config` contract shapes were discussed and locked in — see "Contract decisions locked for Phase 3.5/5" above.

## Phase 3.5 artifacts (Admin user management, Session 4 + same-chat addendum)

New: `app/routers/admin.py`, `alembic/versions/0007_users_is_active.py`, `alembic/versions/0008_max_admins_trigger_active_and_update.py`, `tests/test_admin_endpoints.py` (25 tests: 19 from the initial build, 6 added for the `confirm_email` addendum), `.gitignore` (found missing entirely, added).
Modified: `app/models.py` (`User.is_active`), `app/schemas.py` (`UserCreateRequest`/`UserResponse`/`UserListResponse`/`UserPatchRequest`, the last gaining `confirm_email` in the addendum), `app/deps.py` (`require_user` now also checks `users.is_active`), `app/routers/auth.py` (`/auth/login` rejects a deactivated user's correct password), `app/user_provisioning.py` (`check_admin_cap_or_raise` now counts active admins only), `app/email.py` (`send_admin_welcome_email` → `send_new_user_welcome_email(role_name=...)`), `app/routers/operator.py` (call site updated for the rename), `app/main.py` (admin router registered), `app/crypto.py` (leftover pre-v2.0 docstring fixed, no behavior change), `tests/test_jwt_planes.py` (4 tests updated for `require_user`'s new user-lookup, 2 new tests added), `tests/test_auth_endpoints.py` (1 new test), `README.md` (status checklist, "Known fixes and additions" section, updated for the addendum too).

**Not built yet (unaffected by Session 4 or the addendum):** everything listed above from Phase 3 remains true — no `PATCH /admin/tenant-config`, no `PATCH /user/llm-config`, no `user_llm_config` table migration, no `/query` endpoint. Self-service password change (`PATCH /auth/me/password` or similar) also still not built — confirmed separate, unphased scope, doesn't block Phase 4. `GuardRail_RAG_Analysis.md` also still not edited — see "Still open" above.
# GuardRail RAG — Decision Log Addendum (Session 5, carry into Phase 4 chat)

**Paste this alongside the existing `GuardRail_RAG_Decision_Log.md` at the start of the Phase 4 chat — this is additive, not a replacement.** Upload `GuardRail_RAG_Analysis.md` and `GuardRail_RAG_Design.md` too if deeper context is needed, per the log's own standing convention.

**Status: Phase 3.5 re-verified end-to-end through the actual `docker compose up --build` artifact — the one prior "not proven" gap carried since Session 3. 74/74 passing, on the real deployable image, for the first time. Four Phase 4 design decisions locked this session, ready to build from. No code written yet for Phase 4 itself — this session was verification + decisions only.**

---

## What changed this session (Session 5)

### 1. Docker Compose path — fully verified for the first time

Every earlier "Complete, Session N — X/X passing against real infra" claim in the log was true, but "real infra" always meant bare-metal Postgres (Docker Hub unreachable in every prior build sandbox) — never the literal container image `docker compose up --build` produces. This session closed that gap for real:

- `docker compose up --build` → both containers healthy.
- `alembic upgrade head` inside the `api` container → `0001 → 0008` applied cleanly, identical to bare-metal.
- `python -m scripts.seed` → seeded cleanly (see note below on invocation).
- Full test suite → **74 passed, 0 failed, 0 errors**, matching the bare-metal count exactly.
- `db`'s image (`pgvector/pgvector:pg16`) runs **pgvector 0.8.6**, vs. bare-metal's 0.6.0 — a real version gap (skips the entire 0.7.x line). All 74 tests passed identically on both. **Scope of what that proves:** schema, trigger DDL (`0008`), and RBAC-filter behavior are confirmed version-compatible. **Scope of what it does NOT prove:** no test in the current suite exercises real `ivfflat` similarity search (`ORDER BY embedding <-> ...`) — `test_rbac.py` inserts a fixed zero-vector fixture and tests row-level filtering, not ranking. Real similarity-search behavior across these two pgvector versions is unverified because the query doesn't exist in the codebase yet (that's Phase 5 scope). Flag this for a quick empirical check once Phase 4/5 populate and query real embeddings.

### 2. `scripts/seed.py` — not actually broken; my invocation was wrong

Earlier this session I flagged `python scripts/seed.py` failing with `ModuleNotFoundError: No module named 'app'` as a code gap. **Retracted** — the script already has `scripts/__init__.py` (a proper package) and its own docstring, plus `README.md` line 48, both already document the correct invocation: `python -m scripts.seed`. That works cleanly, bare-metal and in Docker, including idempotent re-runs ("Seed data already present — skipping"). No code change made or needed. Worth remembering for the Phase 4 chat: always invoke as `python -m scripts.seed`, not `python scripts/seed.py`.

### 3. `docker-compose.override.yml` — added, real gap this one

Confirmed real: the `Dockerfile` only ever installs `requirements.txt`, never `requirements-dev.txt` — meaning `pytest` was never available inside the actual deployable image until this session. Fixed via a new `docker-compose.override.yml` (Compose auto-merges this on top of `docker-compose.yml`, no flag needed) that installs `requirements-dev.txt` into the running `api` container on start, before launching `uvicorn`. Deliberately **not** folded into the production `Dockerfile` — that would ship test tooling in the deployed image. Verified working end-to-end this session (`pip install` completes in a few seconds, then `uvicorn` starts normally; `docker compose exec api pytest -v` → 74/0/0).

**One live gotcha this surfaced, unrelated to the fix:** running `docker compose exec api pytest -v` immediately after `docker compose up -d` can hit `pytest: executable file not found in $PATH` if `pip install` inside the override's `command:` hasn't finished yet — `up -d` returns as soon as the container *starts*, not once its internal command finishes. Not a bug; just wait a few seconds (or check `docker compose logs api`) before running `exec`.

### 4. Local `.env` staleness, noticed not yet fixed

`docker compose config` showed the running container's environment still has `HF_API_TOKEN: replace-with-your-hf-token` and `HF_EMBEDDING_MODEL: BAAI/bge-small-en-v1.5` — meaning the actual `.env` file on the dev machine still has the old placeholder/stale values, not anything reflecting the `nomic-embed-text-v1.5` decision below. Harmless right now (nothing in Phase 3.5 code reads those variables), but it's a live instance of the doc/config drift already tracked in the main log's "Open: embedding hosting conflict" section — needs updating together with the `requirements.txt`/`config.py` propagation once Phase 4 actually touches the embedding call.

---

## Four Phase 4 design decisions locked this session

| # | Question | Decision | Reasoning |
|---|---|---|---|
| 1 | `project_memory.embedding` column size | Widen from `VECTOR(384)` to `VECTOR(1536)`, zero-pad, add an `embedding_dims` column | Matches `document_vectors`'s existing model-change-resilience pattern exactly — one consistent embedding-storage design across both tables, not two different ones. Model just changed once (bge→nomic) and will likely change again; this absorbs that without a future migration. Needs its own migration (`0009`, following the existing numbering) — not yet written. |
| 2 | Chunking strategy | Start with fixed-size chunking (token-count based), applied per `source_type`; record the real `chunk_strategy` value honestly per row (e.g. `"fixed_500_tokens"`, not something vague) | `chunk_strategy` is already a per-row column specifically so strategy can evolve later without touching existing data — no need to build semantic/structural chunking now. Deliberately a first pass, not a finished design (see reminder below). |
| 3 | `min_role_required` default at document upload | Defaults to `1` (Admin-only, strictest) on every new upload; an Admin must deliberately loosen it afterward via the existing `PATCH /ingest/document/{id}` | Fail closed, not open. Consistent with the project's already-stated reasoning for keeping reclassification Admin-only ("avoid a role that creates content also automatically gaining the power to... reclassify it") — same principle, applied one step earlier, at creation. |
| 4 | Sync vs. background/async ingestion | **Stay synchronous for Phase 4.** Async ingestion explicitly considered and deliberately deferred, not silently dropped. | Matches the project's own established pattern for good-but-not-now ideas (AWS RDS/customer-managed keys, key rotation, the two-model embedding split were all handled this exact way — considered, reasoned about, sequenced later). A job queue is real added complexity (worker process, job-status tracking, polling/notification) disproportionate to this project's stated scope (Railway/Render, demonstrable "in under 5 minutes," no real-time collab). **Revisit trigger, stated explicitly so it isn't forgotten:** if a real large-document upload actually times out or blocks noticeably during Phase 4 testing, that's the signal to build it — not a guess made in advance. |

---

## Reminders for Phase 4 (flagging explicitly, not deciding silently)

- **Chunking's fixed-size-only first pass will look primitive** next to a "designed for extensibility" resume framing — same category as the existing "fixed roles only, no custom role creation" line already sitting in the main log's reminders section. Worth its own explicit note there, not mistaken later for a finished design.
- **Nomic prefix / `trust_remote_code` consistency** — `nomic-embed-text-v1.5` expects `search_document:` / `search_query:` text prefixes depending on which side of retrieval you're on, and typically needs `trust_remote_code=True` in `sentence-transformers`. Neither is a blocker, but both need to land correctly and *consistently* between the ingestion-time embedding call and the (later, Phase 5) query-time embedding call, or retrieval quality degrades silently — no test will catch a wrong/missing prefix, it'll just quietly return worse matches.
- **`GuardRail_RAG_Analysis.md` still not edited** to reflect the Session 4 judgment calls (PATCH-based reactivation, self-target guard) or the `confirm_email` addition — flagged as "genuinely due" back in Session 4's addendum, still not done as of this session. Unrelated to Phase 4 itself, but a real outstanding item.

---

## Genuine remaining blockers before Phase 4 code can be written (all three done in Session 6 — see bottom)

1. **Embedding-model propagation** — not yet executed (decision was made, work wasn't): `requirements.txt` (add `sentence-transformers`, drop pure-API-token framing), `config.py` (`hf_api_token` → local model config, e.g. `embedding_model_name`), `.env`/`.env.example` on the dev machine and in the repo, and both `GuardRail_RAG.md` and `GuardRail_RAG_Design.md` still say "HuggingFace Inference API" / `bge-small-en-v1.5`.
2. **`project_memory` migration (`0009`)** — decision #1 above is locked, migration itself not written.
3. **Ingestion pipeline code** — none of decisions #1–4 above are implemented yet; this session was decisions + verification only, no Phase 4 application code was touched.

Nothing else is blocking — the infrastructure (Docker, migrations, test harness) is now fully proven, and all four open Phase 4 design questions have answers. The Phase 4 chat can start directly on the propagation work and the `0009` migration.

---

## Session 6 — Phase 4 (Ingestion) built and tested

Order: re-verify the inherited state, grep the real code for everything Phase 4 builds on (not just re-read this log), then migrations, then embedding propagation, then chunking + endpoints, then tests. Same "verify before building" discipline as Session 4.

### Verification pass before any Phase 4 code

Unzipped the Phase 3.5 deliverable, built Postgres 16 + pgvector 0.8.0 directly in the sandbox (Docker Hub unreachable, same as every prior session), applied `0001→0008`, ran `pytest`: **74 passed** — matches the claim exactly.

Then grepped the actual code for what Phase 4 was about to build on. **Three real gaps between this log and the shipped schema** (none were behavior bugs — nothing had ingested anything yet — but each would have meant writing ingestion code against tables/columns that silently didn't exist):

1. **`project_memory` did not exist at all.** Session 5 described "widening" its embedding from `VECTOR(384)` to `VECTOR(1536)`; there was no table to widen (no model, no migration, only comments mentioning it). **Migration `0009`** creates it fresh at `VECTOR(1536)` + `embedding_model` + `embedding_dims`. The `embedding_model` column is an **extrapolation**: Session 5's summary line named only `embedding_dims`, but its own reasoning ("one consistent embedding-storage design across both tables") only holds with the full three-column pattern. No `chunk_strategy` here — a memory entry is one embedded note, not a chunked document.
2. **`documents.source_type` did not exist** (in the Design doc's schema from day one, never migrated). **Migration `0010`**: added nullable → backfilled existing fixture rows to `'markdown'` (arbitrary but valid; they're metadata-only RBAC fixtures) → `NOT NULL` + `CHECK IN ('code','pdf','markdown')`.
3. **`document_vectors.chunk_strategy` did not exist** (this log said "already a per-row column"). **Migration `0011`**: same nullable → backfill → `NOT NULL`, backfilled to `'legacy_pre_phase4'` so a future reader can tell such rows predate real ingestion. Free text, no `CHECK` — the point of the column is letting the strategy change over time.

### Embedding propagation — the "Open: embedding hosting conflict" is closed

Self-hosted, in-process: **`nomic-embed-text-v1.5`** via `sentence-transformers` (taken from Session 5's Nomic-prefix reminder — confirm if that wasn't the final pick from the model-selection chat). `app/embeddings.py` (new): `EmbeddingBackend` protocol, `SentenceTransformerBackend` (lazy-loaded once per process, `trust_remote_code=True`), `EmbeddingService` (applies `search_document: ` / `search_query: ` prefixes — both constants side by side so they can't drift — zero-pads to 1536, returns `embedding`/`embedding_model`/`embedding_dims` together). `app/config.py`: `hf_api_token`/`hf_embedding_model` **removed** (not renamed), replaced by `embedding_model_name`. `.env.example` and `requirements.txt` updated (`sentence-transformers==3.1.1`, `einops==0.8.0`, `pypdf==5.0.1`, `torch==2.4.1+cpu` via `--extra-index-url https://download.pytorch.org/whl/cpu` — plain `torch` pulls a multi-GB CUDA stack this project has no use for).

Still stale, by design left for the project owner: the local dev `.env` (Session 5 item), and **`GuardRail_RAG.md` / `GuardRail_RAG_Design.md` still say "HuggingFace Inference API" / `bge-small-en-v1.5`** — those two docs were not edited (they're the owner's canonical docs; offer stands to produce corrected copies).

### Chunking (`app/chunking.py`) — first pass, explicitly

`chunk_strategy_label()` → `"fixed_500_tokens"` (the literal example from Session 5). "Token" = whitespace-delimited word count, **not** the embedding model's real subword tokenizer (using it would mean loading the model just to chunk). Same fixed-size strategy for all three `source_type`s; the one structural difference: `code` is chunked along line boundaries so a line is never split in half. 50-token overlap (an implementation detail, not separately specified). **Known limitation:** a single line longer than the target (e.g. minified code) becomes one oversized chunk.

### Ingestion (`app/routers/ingest.py`)

- `POST /ingest/code` (Admin/Developer; JSON body, raw text) and `POST /ingest/document` (Admin/Developer; multipart PDF/markdown, `source_type` inferred from the filename extension, not a client-supplied content-type). Trainee cannot ingest (401, same convention as every other role gate).
- `PATCH /ingest/document/{id}` — **Admin-only**, changes `min_role_required`. `DELETE /ingest/document/{id}` — **Admin-only, hard delete** (differs from `DELETE /admin/users`' soft deactivation on purpose: nothing references a document for attribution; `document_vectors` is the only FK and is already `ON DELETE CASCADE`). Both tenant-scoped with the generic 404 for nonexistent-vs-other-tenant.
- **`min_role_required` defaults to `1`** (Session 5 #3) at the ORM level, at the DB level (migration `0010` `server_default`), and in the endpoints. Existing rows untouched.
- Synchronous end to end (Session 5 #4): extract → chunk → embed the whole batch in **one** model call → one transaction for the `Document` + all its `DocumentVector` rows.
- **Added without being asked:** `INGEST_MAX_FILE_SIZE_MB` (default 20) upload cap — sync ingestion has no worker to hand a huge file to. Same revisit trigger as Session 5 #4.
- **Scope boundary, not decided silently:** `project_memory`'s *table* was built (Session 5 locked it as Phase 4); the `POST`/`GET /project-memory` *endpoints* were not — they aren't listed under Phase 4 in the build-phase list. Needs an owner call on which phase owns them.

### Tests — 17 new (91 total), real Postgres over ASGI

`tests/test_ingest_endpoints.py`: role gates on both ingest endpoints, `min_role_required` default + override, empty content, real markdown, **real PDF with the extracted text verified present in a stored chunk**, unsupported extension, oversized file, reclassify (Admin-only / Developer rejected / out-of-range 422 / cross-tenant generic 404 with a follow-up read proving the row was untouched), delete (Admin-only / Developer rejected / cascade to `document_vectors` proven by re-query / nonexistent 404). The module overrides `get_embedding_service` with a fake backend (8 native dims, far narrower than 1536, so zero-padding is provably exercised). PDF fixture is hand-built minimal PDF bytes — no PDF-writing dependency added. `test_rbac.py` / `scripts/seed.py` fixtures updated for the new `NOT NULL` columns (mechanical fix).

**Verified:** fresh volume `0001→0011` + seed + 91/91; same 91/91 on the incrementally-migrated volume (backfill exercised against real pre-existing rows); `downgrade -1` ×3 then `upgrade head` then 91/91 again. App boot confirmed all four ingest routes registered.

**Not proven (unchanged sandbox limits):** `docker compose up --build`; the real embedding model downloading/loading (no huggingface.co); the `+cpu` torch wheel resolving (no download.pytorch.org — only the plain pin was dry-run-resolved against PyPI, no conflicts); real SMTP. Recommended: `docker compose up --build` then one real `POST /ingest/document` against the live model — the one thing dependency injection can't substitute for. Also still owed from Session 5: real `ivfflat` similarity-search behavior across pgvector 0.6/0.8 (no query exists until Phase 5).

### What the tests do and do NOT cover (asked directly by the project owner — recorded honestly)

**Real, not mocked:** HTTP layer (ASGI), Postgres + pgvector, role/tenant boundaries, cascade deletes, real PDF byte parsing. **Substituted:** the embedding step only (fake backend).

**Not covered — known, not "every imaginable scenario" (that bar doesn't exist):**
- Two gaps found by *reading* the code, not by a test, verified this session and **not yet fixed**: (a) `app/routers/ingest.py` has **no `DBAPIError` handling** around its `flush`/`commit`, unlike `admin.py` (lines ~138, ~271) — a DB-level failure mid-ingest would surface as an unhandled 500 rather than the project's safe-error contract; (b) PDF extraction catches only `pypdf.errors.PdfReadError` — that family *does* include encrypted-without-password (`FileNotDecryptedError` is a subclass, checked), but exceptions outside it (e.g. `DependencyError`, or a raw error from a strangely malformed file) would also be unhandled 500s. No test uses a corrupt/encrypted PDF at all.
- Never exercised: concurrent ingests / reclassify-vs-delete races on one document; file size *exactly at* the cap (only "clearly over" tested); missing/garbage `Authorization` header directly on the ingest routes (same `require_role` dependency is proven elsewhere, so low risk, but not exercised here); very large documents (thousands of chunks — batch size, time, memory); embedding-call failure/timeout mid-batch; anything about the real model's behavior.

### Open items — to be handled later in this thread (per project owner), not addressed in Session 6

1. **No `token_blacklist` table and no `/auth/logout` endpoint exist anywhere**, despite Phase 2 being recorded complete with "token blacklist" (grepped the whole tree; only comments). Security-plane gap; Phase 6's listed scope doesn't include it either.
2. The two `ingest.py` error-handling gaps above.
3. `POST`/`GET /project-memory` — which phase owns them.
4. `GuardRail_RAG.md` / `GuardRail_RAG_Design.md` still contain the HuggingFace-Inference-API language; `GuardRail_RAG_Analysis.md` still not updated for Session 4's judgment calls.
5. Pre-existing, untouched deviations from the Design doc: `documents` (not `document_metadata`) table name; roles are an `int` column on `users` with a `CHECK`, no separate `roles` table.

### Phase 4 artifacts

New: `app/embeddings.py`, `app/chunking.py`, `app/routers/ingest.py`, `alembic/versions/0009_project_memory.py`, `0010_documents_source_type_and_default.py`, `0011_document_vectors_chunk_strategy.py`, `tests/test_ingest_endpoints.py` (17 tests).
Modified: `app/models.py` (`Document.source_type`, `min_role_required` default, `DocumentVector.chunk_strategy`, `ProjectMemory`), `app/schemas.py`, `app/config.py`, `app/main.py`, `requirements.txt`, `.env.example`, `scripts/seed.py`, `tests/test_rbac.py`, `README.md`.

---

## Session 7 — token blacklist (the real Phase 2 gap), fixed

Triggered by a direct question: "is it really true that token blacklisting was never set up?" Answered by re-verifying against the *original Phase 2 deliverable* itself (unzipped and re-run), not just the current codebase — confirmed the gap traces all the way back: no `token_blacklist` table in migration `0001`, no `/auth/logout` route (only login existed), and no `jti` claim minted by `_encode()` at all, so there was nothing in a token's own shape for a blacklist to reference. This was promised in `GuardRail_RAG.md`'s own build-phase description ("Security core: ...JWT plane separation, token blacklist") and silently never built, at any phase, until this session. Built on top of the Phase 4 codebase (Session 6), per the project owner.

Also fixed in the same session, flagged earlier as a related gap: the missing JWT-expiry test (`decode_token()` was already correctly rejecting expired tokens — hand-verified before this session — it just had no automated test proving it, unlike every forged-token scenario next to it).

### What was built

- **`jti` added to every token** (`app/security.py::_encode()`). Tokens minted before this change have no `jti`; `app/deps.py::_is_blacklisted()` treats a missing `jti` as "can't be individually blacklisted" rather than erroring — backward compatible with any token already in flight, not a special case.
- **`token_blacklist` table** — migration `0012`, `app/models.py::TokenBlacklist`. Matches the Design doc's schema exactly: `token_jti` alone as primary key (a `jti` is already globally unique), `tenant_id` nullable (operator tokens carry none).
- **`POST /auth/logout`** (`app/routers/auth.py`) — blacklists the presented token's `jti`. Idempotent by design: logging out an already-blacklisted token, or calling logout twice with the same token, returns the same `204`, not an error — `require_any_plane` (the new decode-only dependency gating this route) deliberately does **not** itself check the blacklist, so a second logout call isn't rejected before it even reaches the idempotency check.
- **Enforcement in both `require_user` and `require_operator`** (`app/deps.py`) — a blacklisted token is rejected on its very next request, reusing the same generic `_AUTH_ERROR` every other invalid-token case uses (no distinct "this token was blacklisted" message — that would only be useful to an attacker holding it).
- **`require_operator` became async + DB-backed** to run this check — it was sync with no DB dependency at all before this session. Same "mechanical fix" transition `require_user` went through in Phase 3 for its first DB check. The two `test_jwt_planes.py` tests that called it directly as a sync function were updated to `await` it with a `db_session`.
- **Scope interpretation, flagged rather than assumed:** `/auth/logout` accepts *either* plane's token, not just a tenant user's. The Analysis doc lists it under "Auth (tenant users)" specifically, but the mechanism and the table itself (nullable `tenant_id`) are plane-agnostic, and there's no separate `/operator/logout` anywhere in the documented API surface. Read as "any token is revocable," not "only user tokens are" — straightforward to split into two routes later if that reading turns out to be wrong.
- **The missing expiry test added**: `tests/test_jwt_planes.py::test_expired_token_is_rejected`, hand-crafting an already-expired token (not via `create_user_token`, which always sets a future expiry) the way a real, legitimately-issued-but-stale token actually looks.

### Tests — 10 new in `tests/test_token_blacklist.py`, plus 1 in `test_jwt_planes.py` (102 total)

End-to-end over real HTTP, not just the dependency function in isolation: mint a token, hit a genuinely protected route to confirm it works, call `POST /auth/logout`, hit the *exact same route with the exact same token* again, confirm it's now rejected. Covers: user-plane logout + rejection, operator-plane logout + rejection (via a real `PATCH /operator/tenants/{id}` call before and after), idempotent double-logout, one session's logout not affecting a different token for the same user, the stored row's `tenant_id`/`expires_at` matching the token's own claims exactly (user token: real `tenant_id`; operator token: `NULL`), logout with no token at all (401), logout on a hand-crafted pre-migration token with no `jti` (graceful `204` no-op, confirmed no row gets written under a null/empty key), plus two direct dependency-level tests mirroring `test_jwt_planes.py`'s existing style.

**Verified:** full suite (102/102) run three ways — incrementally against the already-migrated Phase 4 database, from a dropped-and-recreated fresh volume (`0001→0012` clean, reseeded), and after a full `alembic downgrade -1` / `upgrade head` round-trip of migration `0012` specifically. App boot confirmed `/auth/logout` registered alongside every other route.

**Not proven (unchanged sandbox limits, same as every prior session):** `docker compose up --build`, real SMTP, the real embedding model. None of those are touched by this session's change.

### Phase 4's genuine remaining blockers — status unchanged by this session

The two `app/routers/ingest.py` error-handling gaps (missing `DBAPIError` handling, narrow `PdfReadError`-only catch) and the `POST`/`GET /project-memory` scope-boundary question from Session 6 are **not** addressed here — this session was scoped specifically to the token blacklist gap, per the project owner. Still open, still carried forward.

### Session 7 artifacts

New: `alembic/versions/0012_token_blacklist.py`, `tests/test_token_blacklist.py` (10 tests).
Modified: `app/security.py` (`jti` claim), `app/models.py` (`TokenBlacklist`), `app/deps.py` (`_is_blacklisted`, `require_any_plane`, blacklist check in `require_user`/`require_operator`, `require_operator` now async+DB-backed), `app/routers/auth.py` (`POST /auth/logout`), `tests/test_jwt_planes.py` (2 tests updated for `require_operator`'s new signature, 1 new expiry test), `README.md`.

---

## Session 8 — seven open items reviewed, decided; two changes planned but not yet coded

Decisions-only session, per the project owner's request: record what's decided before touching any code, so the plan can be confirmed first. Nothing in this section's "planned" items has been implemented yet.

### 1. Does `pip install -r requirements.txt` download the embedding model? — No, clarified, no decision needed
`pip install` only installs the Python packages (`sentence-transformers`, `torch`, `einops`, `pypdf`). The actual `nomic-embed-text-v1.5` weights are fetched separately, lazily, on first real use — `app/embeddings.py::SentenceTransformerBackend._ensure_loaded()` only triggers a download the first time an embedding is actually computed (e.g. the first real `POST /ingest/code` call), not at install time and not at app startup. First call will be slow (download + load); every call after is fast (cached locally, held in memory for the process's lifetime). Noted here only so a future session doesn't assume `pip install` alone proves the model path works — it doesn't; see the still-open "real embedding model never verified" item.

### 1b. Does the embedding model run in its own container? — No, clarified, no decision needed
Raised as a question in a follow-up message: whether nomic-embed-text-v1.5 was meant to run in a separate container, reached via requests from the `api` container, for data-isolation reasons. Checked against the record: that container question was real, but for the **generation** model (self-hosted IBM Granite via Ollama) — explicitly ruled moot once GuardRail decided to never own a generation model at all ("Major correction" section above). The **embedding** model's decision has been on file since before this thread started and was never in question: "Docker Compose (2 services: api, db — no model-serving container...) · self-hosted... via sentence-transformers inside the `api` container for embeddings only." In-process, same container, no network hop for the model call at all. Worth the distinction for the security reasoning specifically: in-process is *stricter* than a separate container would be here, since a sibling container would mean chunk text crossing the Docker-internal network to reach it, where in-process it never leaves the handling request's own memory. What *does* cross a network, once, is the model's weights downloading from Hugging Face on first use — software, not any tenant's data. No architecture change made; flagging only in case a future session second-guesses this again.

### 2. `docker compose up --build` smoke test — confirmed, project owner's to run
No change needed from this session. Still the same recommended smoke test carried since Session 5 (now also covering Phase 4/Session 7's new dependencies): `docker compose up --build`, then `pytest` inside the `api` container.

### 3. Real SMTP — confirmed, Phase 6 as already planned
No change. Dev-mode outbox remains the only verified path until Phase 6.

### 4. Doc corrections (`GuardRail_RAG.md`, `GuardRail_RAG_Design.md`, `GuardRail_RAG_Analysis.md`) — **done**

Project owner said they'd attach updated copies; none arrived with that message, but the actual files were uploaded in a later message and confirmed byte-identical to the `/mnt/project/` copies already used throughout this thread, so the plan below applied directly without changes.

Edits made:
- **`GuardRail_RAG.md`**, line 31: `- **Embeddings:** BAAI/bge-small-en-v1.5 (HuggingFace Inference API, 384-dim)` → describe the actual shipped stack instead: self-hosted `nomic-embed-text-v1.5` via `sentence-transformers`, no external API call.
- **`GuardRail_RAG_Design.md`**, line 5 (the Stack line): `HuggingFace Inference API (BAAI/bge-small-en-v1.5) for embeddings` → same correction, in context with the rest of that line.
- **`GuardRail_RAG_Analysis.md`**: grepped for "reactivat", "self-target", "confirm_email" — zero matches anywhere in the file, confirming Session 4's two judgment calls and the `confirm_email` addition were never folded in, exactly as flagged back in Session 4's addendum. Planned additions:
  - §1's role table / `PATCH /admin/users/{id}` description (currently: "full lifecycle management... Belongs to Phase 3.5") — add that `is_active` accepts both directions (deactivate and reactivate), that reactivation additionally requires a matching `confirm_email`, and that an Admin cannot target their own user id via `PATCH`/`DELETE`.
  - §6's decision-log table — add rows for the three Session 4 items (PATCH-based reactivation, self-target guard, `confirm_email` requirement), matching the table's existing row format.

### 5. `/auth/logout` plane split — **done** (Session 9)

Confirmed: split into `POST /auth/logout` (user-plane only) and a new `POST /operator/logout` (operator-plane only), replacing the current single plane-agnostic endpoint. Planned implementation:
- `app/deps.py`: replace `require_any_plane` with two narrower decode-only dependencies (plane-checked, no blacklist check, no DB — same reasoning as before: logout must stay idempotent even against an already-blacklisted token). A shared `blacklist_token(db, payload)` helper factored out so both routes insert into `token_blacklist` identically rather than duplicating that logic.
- `app/routers/auth.py`: `POST /auth/logout` now rejects an operator token outright (401) instead of accepting it.
- `app/routers/operator.py`: new `POST /operator/logout`, symmetric, rejects a user token.
- Tests: `tests/test_token_blacklist.py`'s existing operator-logout test moved to `POST /operator/logout`; two new tests added confirming each route now rejects the *other* plane's token — the same plane-separation guarantee the rest of the app already has, extended to logout. Built in Session 9 exactly as planned here.

### 6. `POST`/`GET /project-memory` ownership — **decided: Phase 5, project owner's to pick up**
Confirmed. No code change now. Carried forward as Phase 5 scope, same table (migration `0009`) already in place from Session 6.

### 7. Admin notification mechanism when the tenant's company LLM key fails at query time — options recorded, **final pick still open, Phase 5 scope either way**

Phase 5 build item (query/retrieval + generation) — nothing to code now. The *rule* ("Admin gets told immediately") has been settled since the original design-doc-correction session; only the *delivery mechanism* was ever left open. Project owner's request: an in-app notification if an Admin currently has the application open, falling back to email only if every Admin in the tenant appears offline. Two options discussed:

- **Option A (presence-aware, matches the request literally):** Always write the in-app notification (a flag/row an Admin sees on their next request). Additionally send an email, but only if no Admin in the tenant shows recent activity — e.g. a `users.last_seen_at` column touched on every authenticated request (or, cheaper, reusing existing `query_log` activity as a rougher proxy, though that only captures `/query` calls specifically, not all app activity). Needs a recency threshold (e.g. "active within the last 15 minutes") to decide "offline."
- **Option B (always both — the alternative the project owner invited):** Write the in-app notification *and* send the email, unconditionally, every time. No presence tracking, no new column, no threshold to tune. Slightly more email traffic on the rare occasion an Admin genuinely was already online and would have seen the in-app flag anyway — but given the decision log's own stated reasoning for this rule in the first place ("a broken company default blocks every user who doesn't have their own override, so this needs to surface promptly, not be discovered accidentally"), over-notifying is the safer failure mode for something this disruptive, not a flaw.
- **Explicitly not recommended:** real-time presence via WebSockets/SSE. Would answer "is an Admin's browser open right now" precisely, but introduces a persistent-connection layer this project has no other use for and never scoped (FastAPI + Docker Compose + Railway/Render, no mention of realtime anywhere else) — disproportionate complexity for this one notification.

**Decided: Option B** — always write the in-app notification and send the email, unconditionally, no presence detection. Confirmed by the project owner (Session 8, second pass). No `last_seen_at` column, no activity threshold, no "is an Admin online" logic needed. Still Phase 5 build scope (there's no `/query` endpoint yet for this to attach to) — recorded here so the mechanism question doesn't reopen when that phase starts.

---

## Session 9 — event-loop blocking fixed, crash isolation added, `/auth/logout` split by plane, embedding-container question resolved

Triggered by a follow-up question about embedding architecture (was a separate container for the embedding model ever decided?) that surfaced two real, separate concerns worth distinguishing: data-exposure surface (answered — no, never decided, and in-process is the stricter choice, not a looser one, since no private chunk text crosses any network boundary at all) and availability/fault isolation (a genuinely new, valid point, addressed below).

### Clarified, no code change: embedding model was never meant to run in a separate container

Re-confirmed against the record precisely because the project owner pushed back on this directly. The "separate container" conversation that actually happened in this project's history was for the **generation** model (self-hosted IBM Granite via Ollama) and was explicitly ruled moot once GuardRail decided to never own a generation model at all. The **embedding** model's decision has been on file since before this thread started: "Docker Compose (2 services: api, db — no model-serving container...) · self-hosted... via sentence-transformers inside the `api` container for embeddings only." In-process, not a sibling container. Logged as its own item (1b) under Session 8 above; no architecture change made as a result of the clarification itself.

### Found and fixed: embedding (and PDF-extraction) calls were blocking the event loop

While working through the project owner's fault-isolation question, re-read `app/routers/ingest.py` and found a real, present bug, not a hypothetical: `embedding_service.embed_documents(chunks)` was called directly inside an `async def` function, but the function underneath it (`model.encode(...)`) is a plain blocking, CPU-bound call with no async API. Python's single-threaded event loop means that call froze the *entire worker* for its duration — not just the ingesting request, every other in-flight request on that worker (login, admin, another tenant's anything) would stall until it finished. Same issue, smaller scale, in `_extract_pdf_text` for large PDFs.

**Fixed:** both call sites now go through `await asyncio.to_thread(...)`, moving the blocking work onto a thread pool instead of the event loop. No other behavior change; `tests/test_ingest_endpoints.py`'s full 17 re-passed unchanged.

### Added: multiple uvicorn workers, for real crash/OOM isolation

The project owner's stated concern — "if the embedding shuts down or comes with an error, there's a chance it will stop the entire server" — is real for one specific failure mode: a memory exhaustion (OOM) in `torch` during a large embed batch can get the whole process killed by the OS, not just that one request, since auth/admin/ingestion all currently share one process. `to_thread` above does not fix this (same process, same memory space) — only a process boundary does.

**Fixed, proportionately:** `Dockerfile`'s production `CMD` now runs `--workers 2` instead of 1. If one worker gets OOM-killed, the other keeps serving everything else while the process manager restarts the dead one. Deliberately not higher than 2: each worker loads its own independent copy of the embedding model on first use (the singleton in `app/embeddings.py` is per-process, not shared across workers), so worker count directly multiplies that memory cost — documented in the Dockerfile's comment as a tuning note, not a fixed ceiling. `docker-compose.yml`'s local-dev command is unchanged (`--reload`, single worker) — uvicorn's `--reload` doesn't support multiple workers, and hot-reload is worth more locally than crash isolation; commented in place so this isn't "fixed" into breaking local dev later.

**Not done, named as a real remaining option:** true process-boundary isolation of the embedding dependency itself (a separate embedding microservice/container), which neither of the two fixes above provides — they isolate *crashes*, not the *dependency surface* (a `torch` CVE in a separate container still can't reach the Fernet key or DB credentials the way it theoretically could in-process, even if sandboxed to a thread). Explicitly recommended *against* building this now: it's materially more infrastructure (an internal API between containers, health checks, retry/backoff in the caller, possibly its own auth) than this project has anywhere else, for a narrower threat than the OOM case `--workers 2` already covers. Worth noting given this is a portfolio project specifically framed around "demonstrating hands-on understanding of *why* systems are architected this way" (`GuardRail_RAG.md`) — a deliberately isolated embedding service could be a legitimate, well-justified demonstration piece in that context, not just defensive engineering, which is a different calculus than pure production necessity. Flagged as a real, named option for whenever the project owner wants to revisit it; not decided either direction, not built.

### `/auth/logout` split by plane

Session 7 built one `POST /auth/logout` that accepted either an operator's or a user's token, flagged at the time as an interpretation beyond the Analysis doc's literal "Auth (tenant users)" scoping. Project owner chose the stricter reading (Session 8, option B): split into `POST /auth/logout` (user-plane only) and new `POST /operator/logout` (operator-plane only), matching the plane separation every other route in the app already has.

- `app/deps.py`: `require_any_plane` removed, replaced by `require_user_plane_logout` and `require_operator_plane_logout` — same decode-only, no-blacklist-check reasoning as before (logout must stay idempotent against an already-blacklisted token), now each also plane-checked. Shared insert logic factored out into `blacklist_token(db, payload)` so both routes write to `token_blacklist` identically rather than duplicating it.
- `app/routers/auth.py`: `POST /auth/logout` now rejects an operator token outright (401) via the new dependency.
- `app/routers/operator.py`: new `POST /operator/logout`, symmetric.
- Tests: the existing operator-logout test in `tests/test_token_blacklist.py` moved to `/operator/logout`; two new tests added (`test_auth_logout_rejects_an_operator_token`, `test_operator_logout_rejects_a_user_token`), each also confirming the rejected token genuinely wasn't blacklisted as a side effect of the failed attempt.

### Verified

Full suite (104/104: 102 carried over + 2 new plane-separation tests) run against the incrementally-migrated database and again from a dropped-and-recreated fresh volume (`0001→0012`, reseeded) — no new migration this session, so this mainly re-confirms nothing regressed. App boot confirmed both `/auth/logout` and `/operator/logout` registered, `/auth/logout` no longer listed as accepting both planes.

**Not proven (unchanged sandbox limits):** `docker compose up --build` specifically with the new `--workers 2` CMD — recommended as part of the same smoke test already carried since Session 5, now also confirming the multi-worker startup behaves as expected (each worker binds correctly, no port conflict, logs distinguish which worker is which).

### Session 9 artifacts

New: none (no new files).
Modified: `app/routers/ingest.py` (`asyncio.to_thread` for both embedding and PDF extraction), `Dockerfile` (`--workers 2`), `docker-compose.yml` (comment explaining why local dev stays single-worker), `app/deps.py` (`require_any_plane` replaced by two plane-specific logout dependencies, new `blacklist_token` helper), `app/routers/auth.py` (logout now user-plane only), `app/routers/operator.py` (new `POST /operator/logout`), `tests/test_token_blacklist.py` (one test moved, one test's token plane fixed, two new tests).

---

## Session 10 — the three previously-deferred ingest gaps, actually fixed

Project owner's instruction: fix the `DBAPIError` handling gap, the narrow PDF exception catch, and the listed untested surface area (concurrent ingests, exact file-size boundary, very large documents, embedding-call failure mid-batch, missing-auth-header on ingest routes) — all flagged in earlier sessions, deliberately deferred until now.

### `DBAPIError` handling added to `app/routers/ingest.py`

All three of `_embed_and_store`, `reclassify_document`, `delete_document` now wrap their writes in `try`/`except DBAPIError`/`rollback`, via a new shared `_db_write_failed()` helper — same shape `admin.py`/`operator.py` already had. **Classified as `500 server_error`, not `400 invalid_input`** like `admin.py`'s equivalent, documented as a deliberate difference in the helper's own docstring: `admin.py`'s `DBAPIError` cases are specifically races against a known business constraint (the admin cap, username uniqueness) — genuinely "bad input, right now." Nothing in `ingest.py`'s writes has that shape (every constraint a document/chunk insert could trip — `min_role_required`'s `CHECK`, `source_type`'s `CHECK` — is already validated before reaching the DB), so a `DBAPIError` here more likely means a transient infrastructure problem; `500` is the honest classification, not a copy-paste of `admin.py`'s choice.

### PDF exception handling widened in two steps

1. `PdfReadError` → `pypdf.errors.PyPdfError` (the actual common base every `pypdf`-specific exception inherits from — though `PdfReadError`'s own subtree already covered the most likely real cases, including encrypted-without-password via `FileNotDecryptedError`).
2. A broad `except Exception` fallback, specifically at this one untrusted-file-parsing boundary. Real experimentation first (truncating at various byte offsets, corrupting `/Length`, pointing `/Root` at a nonexistent object) found `pypdf` 5.0.1 quite defensive — none of these tripped anything outside `PyPdfError`. Rather than conclude "no broader catch needed," added it anyway (a sufficiently adversarial input could still find something pypdf's own hierarchy doesn't cover) and proved it with an injected non-`PyPdfError` exception (`IndexError`, via monkeypatching `pypdf.PdfReader` itself) rather than relying on a specific corrupted-byte sequence that happens to trip a particular internal pypdf 5.0.1 bug — more robust across pypdf version upgrades, and honestly demonstrates the catch is doing real work.

### Found while testing, not asked for directly, fixed anyway: embedding-call failures were also unhandled

Same category of gap as the two above — `embedding_service.embed_documents(...)` (inside the `asyncio.to_thread` call added in Session 9) wasn't wrapped in anything. A real backend failure (OOM, a corrupted/partial model download, a library bug) would have propagated raw instead of this project's safe-error contract. New `_embedding_failed()` helper, same `500 server_error` shape as the DB one. Flagged here explicitly since it wasn't part of the project owner's original three-item list.

### 8 new tests in `tests/test_ingest_endpoints.py` (112 total)

- Missing `Authorization` header on all four ingest routes.
- A malformed PDF → safe `400` (baseline `PyPdfError` coverage).
- `pypdf.PdfReader` monkeypatched to raise `IndexError` → safe `400` — the test that actually proves the broadened catch matters, since real malformed bytes couldn't be coaxed outside `PyPdfError` in testing (see above).
- `AsyncSession.commit` monkeypatched to raise `DBAPIError` → safe `500`, with a follow-up query confirming no partial `Document` row was left behind.
- A fake embedding backend that always raises → safe `500`, same no-partial-row confirmation.
- File size **exactly** at the configured cap (accepted) vs. one byte over (rejected) — the existing oversized-file test only ever checked "clearly over."
- A ~12,000-line document, chunk count and content coverage verified at scale. **First draft used content with no newlines at all and got exactly 1 chunk** — not a test bug caught by luck, but a live demonstration of `app/chunking.py`'s own documented limitation: a single line longer than the target becomes one oversized chunk rather than being split further, because `code` is chunked by line boundaries. Fixed by using realistic multi-line content (one statement per line), which is what the test was actually meant to exercise.
- Two ingests to the same tenant run concurrently via `asyncio.gather` — both succeed independently with correct, non-cross-contaminated chunks.

### Verified

Full suite (112/112: 104 carried over from Session 9 + 8 new) run against the incrementally-migrated database and again from a dropped-and-recreated fresh volume (`0001→0012`, reseeded) — no new migration this session, nothing in the schema changed.

**Not proven (unchanged sandbox limits):** same list as every prior session — `docker compose up --build`, the real embedding model, the `+cpu` torch wheel, real SMTP.

### Session 10 artifacts

New: none (no new files).
Modified: `app/routers/ingest.py` (`DBAPIError` handling via `_db_write_failed()`, widened PDF exception handling, `_embedding_failed()` wrapping the embedding call), `tests/test_ingest_endpoints.py` (8 new tests), `README.md`.

### What's left — genuinely nothing flagged as an open gap in ingestion right now

Both items the project owner asked for are done; the embedding-failure gap found along the way is also done. The only ingestion-adjacent items still open are the ones already recorded as deliberate scope boundaries, not gaps: `POST`/`GET /project-memory` endpoints (table exists, Phase 5 per the project owner), and the named-but-undecided separate-embedding-container option from Session 9.
