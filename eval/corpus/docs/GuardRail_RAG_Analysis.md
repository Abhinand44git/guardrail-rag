# GuardRail RAG — Analysis

**Phase:** Analysis (SDLC step 2 of 5 — Planning → **Analysis** → Design → Development → Testing/Deployment)
**Derived from:** GuardRail_RAG.md v2.0 (corrected)
**Status:** Draft — corrected to reflect the BYO-AI-model architecture; supersedes the original Analysis doc's API surface and NFRs around generation. **Further corrected here** to fix a scope conflict the Decision Log's Session 3 surfaced: `POST /admin/users` was scoped to "Developer/Trainee accounts" only, which contradicted Session 3's decision that existing Admins — not the Operator — create Admins beyond the first. See §2 and §6 below. **Corrected again** to fold in three Development-time (Phase 3.5) decisions that were made and built but never reflected back into this doc: `PATCH /admin/users/{id}`'s `is_active` supporting reactivation as well as deactivation, the Admin self-target guard on `PATCH`/`DELETE`, and the `confirm_email` requirement on reactivation. See §1 and §6.

---

## 1. Roles and permissions — RESOLVED (unchanged from prior revision)

Two-plane identity model, decided to keep the Operator's "no document access" guarantee architectural rather than just a role check:

**Plane A — Operator (global, no tenant)**
- Lives in a separate `operators` table with no `tenant_id` column at all. There is no query path that can join an operator to `document_vectors` or `project_memory`; the isolation is structural.
- Can: create tenants, create the first Admin user for a tenant, deactivate a tenant, adjust `max_admins`.
- Cannot: query, ingest, or read any document/RAG/project-memory content for any tenant. Cannot configure or touch any tenant's or user's AI model settings — model config is entirely a tenant-plane concern.

**Plane B — Tenant users (scoped to exactly one tenant)**
- One user = one tenant = one role. No multi-tenant membership.
- Fixed role names, no per-tenant custom role creation (documented as a possible v2 feature, not built).

| Role # | Name | Can query docs tagged role ≤ | Can ingest documents | Can manage users/roles | Can set company default AI model | Can set personal AI model override | Notes |
|---|---|---|---|---|---|---|---|
| 1 | Admin | 1, 2, 3 (everything) | Yes | Yes, within own tenant — **including creating, deactivating, or removing other Admins**, not just Developer/Trainee (corrected here; see §2, §6) | Yes — via `PATCH /admin/tenant-config` | Yes | **Capped**: `tenants.max_admins` (default 1, up to 5) |
| 2 | Developer | 2, 3 | Yes, own tenant | No | No | Yes | |
| 3 | Trainee | 3 only | No | No | No | Yes | |

Any Admin can act on any other user in the tenant, including another Admin — for example, if the Admin the Operator originally created leaves the company, a different Admin can deactivate or remove that account. No special protection for "the first Admin" as a person; the role is capped by `max_admins`, but any individual holding it is manageable by peers the same way a Developer or Trainee is.

Model-choice permission is deliberately uniform across all three tenant roles — it is not gated by clearance level, because it governs *which AI generates an answer*, not *which documents can be retrieved*. RBAC continues to govern document access identically regardless of which model a user has selected.

- `document_metadata.min_role_required` is per-document.
- Document deletion and `min_role_required` updates: **Admin-only** (new decision — Analysis v1.0 never addressed this). Deliberately not extended to Developers, even for documents they ingested themselves, to avoid a role that can create sensitive content accidentally acquiring the power to also delete or reclassify it.

## 2. API surface (corrected)

**Operator (global, separate auth path — no `tenant_id` claim in JWT)**
- `POST /operator/login` — issues an operator-scoped JWT. *(Renamed here from the shipped `/auth/operator/login` — see Decision Log for the naming-drift note; new code should use this path going forward.)*
- `POST /operator/tenants` — create a tenant (sets `max_admins`). Tenant is created **without** any AI model configured — `/query` remains unusable for that tenant until an Admin sets one.
- `POST /operator/tenants/{id}/admin` — create the first Admin user for that tenant. Password is not set by the Operator directly — a random password is generated and emailed to the new Admin (minimal send-only implementation now; full reset flow deferred to Phase 6 — see NFR note below).
- `PATCH /operator/tenants/{id}` — activate/deactivate a tenant, adjust `max_admins`. Does **not** touch AI model config — that is Admin-only, via a separate endpoint below.

**Auth (tenant users)**
- `POST /auth/login` — issues tenant-scoped JWT (carries `tenant_id`, `role`)
- `POST /auth/logout` — adds token to `token_blacklist`
- `POST /auth/refresh`

**Query (core RAG)**
- `POST /query` — tenant + role-filtered retrieval → generation, using whichever model resolves for the requesting user (personal override if set, else tenant default). Request body may optionally include a model switch (`llm_provider`, `llm_model_name`, `llm_api_key_ref`); if present, the key is validated immediately and, only if valid, persisted as the user's new personal default before the query proceeds — see Design §3 for the full contract. Response includes a `model_used` field and, when a switch or fallback occurred, an explanatory note.
- `GET /query/history` — in scope (see Decision Log; reopened from the original "nice-to-have" status)

**Ingestion**
- `POST /ingest/code`
- `POST /ingest/document` — PDF/markdown
- `DELETE /ingest/document/{id}` — **new**, Admin-only
- `PATCH /ingest/document/{id}` — **new**, Admin-only, updates `min_role_required`

**Project memory (SDLC-aware)**
- `POST /project-memory` — save a decision, tagged with SDLC phase, records `created_by_user_id`
- `GET /project-memory?phase=&project=` — dual retrieval scoped by tenant/project/phase; Admin sees all entries, Developer/Trainee see only their own

**Admin (tenant-scoped)**
- `GET/POST/PATCH/DELETE /admin/users` — full lifecycle management of tenant users, all roles including Admin. Belongs to **Phase 3.5** (see Decision Log) — it cannot run before Phase 3, since Phase 3 is where the tenant's first Admin is created in the first place.
  - `POST` — request fields: `username`, `email`, `role` (Admin selects Developer/Trainee/Admin). No password field, same reasoning as `POST /operator/tenants/{id}/admin`: a random password is generated and emailed to the new user along with their username, never returned in the response body. If `role` is Admin, this goes through the same admin-cap check as the Operator's endpoint (`app/user_provisioning.py::check_admin_cap_or_raise`, already built and shared — see Decision Log) — just with `max_allowed=tenant.max_admins` instead of `1`. Username unique per tenant, not globally (same `uq_users_tenant_username` constraint, same shared helper).
  - `GET` lists users for the tenant — pagination and role filtering required. Ordering by `created_at` is available (that column already exists) but not required.
  - `PATCH` — two things, either or both in one request (same partial-update pattern as `PATCH /operator/tenants/{id}`): **change a user's role** (e.g. promote Trainee → Developer, or → Admin — the latter goes through the same admin-cap check as `POST`), and/or **toggle `is_active`** (see below). Any Admin can act on any other user in the tenant, **including another Admin** — e.g. if the Admin the Operator originally created leaves the company. **Exception, decided during Development (Phase 3.5, see Decision Log):** an Admin cannot target their own user id via `PATCH` or `DELETE` — without this guard a tenant's sole Admin could deactivate or demote themselves, with the tenant then locked out of Admin access entirely (Operators have no path into a tenant's users at all, by design). Rejected with a safe 400 naming the reason. Self-service password change remains a separate, unaffected endpoint.
    - `is_active` accepts **both directions** — `false` deactivates (see `DELETE` below), `true` reactivates a previously deactivated user. Not stated outright in this doc originally; decided during Development by mirroring `PATCH /operator/tenants/{id}`'s existing `is_active: bool | None` shape, which already supports reactivating a tenant.
    - **Reactivation (`is_active: false → true`) additionally requires `confirm_email`**, matching the target user's stored email exactly (compared after trimming whitespace, no case-folding), checked before the admin-cap check. Not a security boundary — the acting Admin already sees every user's email via `GET /admin/users` — it's a deliberate speed bump against restoring access for the wrong row out of a longer list of deactivated users. A plain deactivation, a role-only `PATCH`, and a no-op `is_active: true` sent to an already-active user are all unaffected by this requirement.
  - `DELETE` — **resolved: soft deactivation, not a hard row delete.** `users` gains an `is_active` column (mirrors `tenants.is_active` — same precedent, not a new mechanism). Reasoning: `project_memory.created_by_user_id` and `query_log.user_id` both need to resolve to a real user row to preserve "who decided/asked what" — the entire point of the SDLC-memory feature. A hard delete either breaks that attribution or forces cascading deletes that destroy the exact history the feature exists to keep. `DELETE` is kept as the HTTP verb for semantic clarity (the caller's intent is "remove this person"), even though it's implemented as `is_active = false`, not a row deletion — worth a code comment so a future developer doesn't assume otherwise.
  - **Resolved, was open:** deactivating an Admin **does** free their `max_admins` slot — that's the actual point of allowing it. This requires `trg_enforce_max_admins` (the DB trigger from migration `0002`) to be updated to count only `is_active = true` Admins, once the `is_active` column exists on `users`, **and** to fire on `UPDATE` (role change or reactivation), not just `INSERT` — the original trigger only covered creation, which both promotion and reactivation would otherwise have silently bypassed. The app-level cap pre-check runs on any transition *into* an active Admin — promotion, reactivation, or both at once in a single request — with the DB trigger as the race-safety net underneath, same belt-and-suspenders relationship the trigger already had with the Operator's admin-creation endpoint.
  - Decision (Decision Log, Session 3): subsequent Admins are created **through this endpoint** by an existing Admin, not repeatedly via the Operator's `/operator/tenants/{id}/admin`. Resolved, locked, and built: `/operator/tenants/{id}/admin` now only ever creates a tenant's first Admin — any tenant with `max_admins > 1` has no way to get a second Admin until this endpoint (`/admin/users`) actually exists. That's the accepted gap this phase closes.
- `PATCH /admin/tenant-config` — **new endpoint.** Admin-only, tenant-scoped. Sets the tenant's company-default AI model (`llm_provider`, `llm_model_name`, `llm_api_key_ref`). The submitted key is tested against the provider immediately; if the test fails, the entire write is rejected — no partial save of provider/model name without a working key. This is the endpoint that makes `/query` usable for a tenant for the first time.
- `GET /admin/audit` — still deferred as a stretch goal (lightweight derived last-modified indicator only — see Decision Log; no new audit table).

**Self-service (tenant users, any role — resolved, separate scope from `/admin/users`)**
- A password-change endpoint (exact route TBD, e.g. `PATCH /auth/me/password`) — **confirmed as user-initiated, not Admin-initiated.** Admin's `/admin/users` above manages *other* people's accounts (create, promote/demote role, deactivate); this is a logged-in user changing their own known password by choice. Distinct from Phase 6's full password-reset flow (which recovers access when logged *out*). Not phased yet — reasonable to build as its own small piece whenever convenient, or bundled into Phase 6 since that's already the password-adjacent phase. Does not block or depend on Phase 3.5.

**User self-service (tenant users, any role except Operator)**
- `PATCH /user/llm-config` — **new endpoint.** Sets or clears the caller's personal AI model override. Same validate-before-save, all-or-nothing rule as the Admin endpoint above.

**Ops**
- `GET /health`

## 3. Data entities (corrected)

- **operators**: `id`, `email`, `password_hash`, `created_at` — no `tenant_id`, by design
- **tenants**: `id`, `name`, `max_admins` (default 1), `created_by_operator_id`, `is_active`, `llm_provider`, `llm_model_name`, `llm_api_key_ref` — **no defaults on the three llm_* columns**; they start null and `/query` is blocked until an Admin sets them via `PATCH /admin/tenant-config`
- **roles**: `id`, `tenant_id`, `role_number` (1–3), `role_name`
- **users**: `id`, `tenant_id`, `email`, `password_hash`, `role_id`, `created_at`, **`is_active`** *(new — resolved this revision, mirrors `tenants.is_active`)*. Deactivating a user (Admin-initiated, `/admin/users`) sets this rather than deleting the row — `project_memory.created_by_user_id` and `query_log.user_id` both need to keep resolving to a real user, which a hard delete would break.
- **user_llm_config** *(new)*: `id`, `user_id` (unique, FK → users), `tenant_id` (denormalized, same rationale as `document_vectors.tenant_id` — keeps the model-resolution lookup a single indexed query), `llm_provider`, `llm_model_name`, `llm_api_key_ref`, `updated_at`. One row per user who has personally overridden their model; absence of a row means "use tenant default."
- **document_metadata**: `id`, `tenant_id`, `title`, `source_type` (code/pdf/markdown), `min_role_required`, `created_at`
- **document_vectors**: `id`, `document_id` (FK, many:1), `tenant_id`, `chunk_text`, `embedding`, `embedding_model`, `chunk_strategy`
- **project_memory**: `id`, `tenant_id`, `project_id`, `sdlc_phase`, `content`, `created_by_user_id`, `created_at`
- **query_log** *(new)*: `id`, `tenant_id`, `user_id`, `project_id`, `query_text`, `created_at`
- **token_blacklist**: `token_jti`, `tenant_id`, `blacklisted_at`, `expires_at`

## 4. Non-functional requirements (corrected)

- **Context budget**: 4,000 tokens per `/query` call, combining retrieved company-knowledge chunks + project-memory chunks. Truncate lowest-relevance chunks first if exceeded; log when this happens.
- **Latency**: previously a fixed p95 < 3s target assumed a GuardRail-controlled default model. That assumption no longer holds — latency now depends entirely on whichever model each tenant/user has configured, which is outside GuardRail's control. Restated as: GuardRail itself must not add more than a small, measurable overhead (RBAC filter + retrieval) on top of whatever the configured model's own latency is; the end-to-end figure is a per-tenant variable, not a fixed NFR.
- **Error messages**: "safe" per the review checklist — no leakage of tenant IDs, role thresholds, SQL structure, or raw provider error text (including AI-provider error responses) in any 4xx/5xx response body.
- **Security**: filter-before-LLM must hold even when the generation call fails, times out, or the configured key is invalid — no fallback path that skips the RBAC filter. Must hold for the Operator plane too (a 403, not a tenant-picker).
- **Model key validation**: any write of an AI model key (tenant-level via Admin, or personal via a user) must be tested against the provider before saving. A failed test blocks the entire write — no partial/half-configured state is ever persisted.
- **Model-failure notification**: if the *tenant's* configured company-default key fails at query time, the Admin is notified immediately (not just the user who happened to be querying), since a broken company key affects every user without a personal override. A user's own personal key failing at query time surfaces a safe, explicit error/warning to that user only, and does not need to notify the Admin.
- **Email (minimal, Phase 3)**: sending a randomly generated password to a newly created Admin is a minimal, send-only implementation in Phase 3 — not the full reset flow, which remains a Phase 6 build. Documented explicitly as a placeholder to be fortified later, not the finished feature.

## 5. Out of scope reminders

No GuardRail-owned/default AI model, no fine-tuning, no autonomous deployment, no real-time collab, no mobile/desktop client, no IDE plugin, no billing, no production hosting beyond the stated Phase 7 target, no non-text file support.

---

## 6. Decision log (this revision)

| Question | Decision | Owner |
|---|---|---|
| Does GuardRail provide a default AI model? | No — every tenant must configure one; Admin sets the company default | You (correction of original intent) |
| Who sets the company-default model? | Admin, via new `PATCH /admin/tenant-config` | You |
| Who can personally override their model? | Admin, Developer, Trainee — all three, same mechanism; Operator excluded | You |
| Precedence | Personal config overrides tenant default when set | You |
| Mid-query model switch | Persists as the user's new personal default going forward; response confirms this | You |
| Where does per-user config live? | Separate `user_llm_config` table, not columns on `users` | Claude's recommendation, confirmed |
| Key validation | Tested immediately on save; failure blocks the entire write; applies identically to tenant and personal config | You |
| Company key failure at query time | Notifies Admin immediately | You |
| First Admin password | Random password via email; minimal send-only build now, fortified in Phase 6 | You |
| Document deletion / reclassification authority | Admin-only | You |
| Admin user management phase | 3.5, after Phase 3 | You |
| Route naming (`/operator/login`) | Renamed here; prior shipped path was `/auth/operator/login` | You |
| Database hosting | Supabase now (Phase 7); AWS RDS/self-hosted documented as a stated future improvement | You |
| Who creates Admins beyond the first? | An existing Admin, via `/admin/users` — not the Operator repeatedly | You (this revision) |
| Can an Admin manage/remove another Admin? | Yes — any Admin, including deactivating/removing the Operator-created first Admin (e.g. if they leave the company) | You (this revision) |
| `GET /admin/users` — pagination/filtering/ordering? | Pagination and role filtering: yes. Ordering: available via `users.created_at` (already exists) but not required | You (this revision) |
| Soft-deactivate vs. hard-delete a user | **Resolved: soft.** `users.is_active`, mirrors `tenants.is_active`. Hard delete would break `project_memory.created_by_user_id`/`query_log.user_id` attribution — the entire point of the SDLC-memory feature | You (this revision) |
| Does removing/deactivating an Admin free their `max_admins` slot? | **Resolved: yes**, that's the point. `trg_enforce_max_admins` needs updating to count only `is_active = true` Admins — concrete Phase 3.5 migration task now, not a hypothetical | You (this revision) |
| `/admin/users` POST fields | `username`, `email`, `role` (Admin picks). No password field — random password generated, emailed with username, same pattern as `POST /operator/tenants/{id}/admin` | You (this revision) |
| Can an Admin change a user's role (e.g. Trainee → Developer)? | Yes — folded into `PATCH /admin/users/{id}` alongside deactivation, same partial-update pattern as `PATCH /operator/tenants/{id}`. Promoting to Admin goes through the same cap check as creating one | You (this revision) |
| Does `PATCH /admin/users/{id}`'s `is_active` field also support reactivating a deactivated user, not just deactivating one? | **Resolved: yes.** Not stated outright anywhere in this doc originally — a judgment call made during Development (Phase 3.5), mirroring `PATCH /operator/tenants/{id}`'s existing `is_active: bool \| None` shape (which already supports reactivating a tenant) rather than inventing a new one. Confirmed with the project owner after the fact | Claude's judgment call (Development, Phase 3.5), confirmed by you |
| Can an Admin target their own user id via `PATCH`/`DELETE /admin/users/{id}`? | **Resolved: no.** Not stated outright anywhere in this doc originally — without this guard a tenant's sole Admin could deactivate or demote themselves, locking the tenant out of Admin access entirely (Operators have no path into a tenant's users at all, by design). Rejected with a safe 400. Confirmed with the project owner after the fact; self-service password change is unaffected, since it's a separate endpoint | Claude's judgment call (Development, Phase 3.5), confirmed by you |
| Does reactivating a user need any extra confirmation beyond the Admin's own authorization? | **Resolved: yes, for reactivation only.** `is_active: false → true` additionally requires `confirm_email` matching the target user's stored email exactly (trimmed, not case-folded — consistent with `/auth/login`'s own email lookup). Not a security boundary (the acting Admin already sees every user's email via `GET /admin/users`) — a deliberate speed bump against restoring the wrong row out of a longer list of deactivated users, the concrete failure mode the project owner had in mind. Two alternatives were considered and set aside: issuing the rehired person a fresh id (rejected — would orphan their `project_memory`/`query_log` history under the old id, defeating the reason soft-deactivation exists); a step-up re-authentication pattern (set aside as heavier than this specific risk warrants — it defends against token theft, not against picking the wrong row) | You (Development, Phase 3.5 addendum) |
| Self-service password change — who initiates it? | **Confirmed: the user themselves, after logging in** — not an Admin action. Separate from `/admin/users` (which only covers Admin-managed accounts) and from Phase 6's forgot-password flow (recovering access while logged out) | You (this revision) |
| Restrict `/operator/tenants/{id}/admin` to first-admin-only? | **Yes — resolved and built.** Any tenant with `max_admins > 1` has no way to get a second Admin until `/admin/users` exists; accepted as a temporary, known gap | You (this revision) |
| Does `/admin/users` let an Admin create another Admin, with the same emailed-password pattern? | Yes, confirmed — identical mechanism to `POST /operator/tenants/{id}/admin` | You (this revision) |

**Next:** Design phase — updated schema (new `user_llm_config` table, nullable tenant `llm_*` columns), updated RBAC/model-resolution sequence diagram, `/admin/tenant-config` and `/user/llm-config` request/response contracts, **plus** (added this revision) `/admin/users` request/response contract and the soft-deactivate-vs-hard-delete decision for tenant users.
