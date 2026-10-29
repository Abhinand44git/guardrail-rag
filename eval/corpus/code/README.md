# GuardRail RAG

Multi-tenant, role-gated RAG API. FastAPI · PostgreSQL + pgvector · JWT · Docker Compose.

## Status: Phase 4 complete + token blacklist (Session 7) + event-loop/crash-isolation fixes + logout plane split (Session 9) + ingest error-handling hardening (Session 10)

- [x] FastAPI skeleton + Docker Compose (api, db)
- [x] Schema as Alembic migrations (operators, tenants, users, documents, document_vectors)
- [x] `trg_enforce_max_admins` DB trigger — counts active admins only, fires on INSERT
      and on promotion/reactivation via UPDATE, not just original creation (migration `0008`)
- [x] JWT issuing/decoding for both identity planes (operator vs. tenant-scoped user)
- [x] Seed script — 1 operator, 2 tenants, fixture users/docs per role and sensitivity level
- [x] RBAC unit test — `app/rbac.py` + `tests/test_rbac.py`, run against real fixtures per role/sensitivity
- [x] Fernet encryption for `llm_api_key_ref` — `app/crypto.py` + `tests/test_crypto.py`
- [x] JWT plane separation test — `tests/test_jwt_planes.py` (plane isolation, forged tokens, alg=none, malformed claims)
- [x] Secret wiring decided (see below) + fail-fast validation on `FERNET_MASTER_KEY`
- [x] `POST /operator/login` (moved from `/auth/operator/login` — see "Known fixes" below)
- [x] `POST /operator/tenants` — creates a tenant with no AI model configured (see migration `0004`)
- [x] `POST /operator/tenants/{id}/admin` — first Admin creation, random password, minimal send-only email
- [x] `PATCH /operator/tenants/{id}` — `is_active` / `max_admins`, with a dedicated grandfather-behavior test
- [x] `POST /admin/users` — Admin creates Developer/Trainee/Admin, same emailed-password mechanism, cap-checked against `tenant.max_admins`
- [x] `GET /admin/users` — paginated, role-filterable, tenant-scoped
- [x] `PATCH /admin/users/{id}` — role change and/or `is_active` toggle (covers both deactivation and reactivation), cap-checked on any transition into an active Admin, reactivation additionally requires a matching `confirm_email`
- [x] `DELETE /admin/users/{id}` — soft deactivation (`users.is_active`), not a row delete
- [x] `users.is_active` enforced immediately, not just at login — `require_user` checks it on every request (mirrors the Phase 3 tenant-deactivation check), and `/auth/login` rejects a deactivated user's correct password
- [x] `POST /ingest/code` — Admin/Developer, raw code as text, chunked + embedded synchronously
- [x] `POST /ingest/document` — Admin/Developer, PDF/markdown file upload, source_type inferred from
      extension, rejects unsupported extensions and oversized uploads before reading them into memory
- [x] `PATCH /ingest/document/{id}` — Admin-only reclassify (`min_role_required`)
- [x] `DELETE /ingest/document/{id}` — Admin-only, hard delete (chunks cascade — see "Known fixes" below
      for why this differs from `DELETE /admin/users`' soft deactivation)
- [x] Fixed-size, token-count-approximate chunking (`app/chunking.py`), honest `chunk_strategy` per row
- [x] Self-hosted embeddings (`app/embeddings.py`) — `nomic-embed-text-v1.5` via sentence-transformers,
      zero-padded to `VECTOR(1536)`, `embedding_model`/`embedding_dims` recorded per row
- [x] New uploads default to `min_role_required=1` (Admin-only, fail-closed) unless the caller
      (Admin/Developer) explicitly loosens it — see migration `0010`
- [x] `project_memory` table created (migration `0009`) — schema only; `POST`/`GET /project-memory`
      endpoints themselves are a separate, not-yet-built scope, see "Known fixes" below
- [x] `POST /auth/logout` + `token_blacklist` table (migration `0012`) — the "token blacklist" item
      from Phase 2's own build-phase description, never actually built until now (Session 7). Every
      token now carries a `jti`; `require_user`/`require_operator` reject a blacklisted one immediately
      instead of it quietly working until `JWT_EXPIRE_MINUTES` elapses. Works for either plane.
- [x] `app/routers/ingest.py`'s embedding and PDF-extraction calls no longer block the event loop —
      both offloaded via `asyncio.to_thread` (Session 9)
- [x] Production container runs `--workers 2` (`Dockerfile`) — an OOM/crash in one worker no longer
      takes the whole server down with it (Session 9)
- [x] `POST /auth/logout` (user-plane) and `POST /operator/logout` (operator-plane) — split from the
      single plane-agnostic endpoint Session 7 originally built (Session 9)
- [x] `app/routers/ingest.py` writes now wrap `DBAPIError` (matching `admin.py`/`operator.py`'s
      existing pattern) — a DB-level failure during ingest/reclassify/delete now rolls back and
      returns a safe `500 server_error` instead of leaking an unhandled exception (Session 10)
- [x] PDF extraction now catches `pypdf`'s full `PyPdfError` hierarchy plus a broad fallback for
      anything outside it — a malformed/adversarial PDF can no longer trip a raw 500 (Session 10)
- [x] Embedding-call failures are now caught and returned as a safe error too — found to be
      unhandled while adding tests for this, fixed alongside (Session 10)
- [x] 8 new tests covering the previously-flagged untested surface: missing auth header on all four
      ingest routes, a malformed PDF, a PDF failure outside `pypdf`'s own exception types (injected),
      a simulated DB failure mid-write, a simulated embedding-backend failure, the file-size cap's
      exact boundary (at-cap accepted, one byte over rejected), a large multi-thousand-line document,
      and two concurrent ingests to the same tenant not cross-contaminating each other (Session 10)
- [ ] Phase 5: Query/retrieval + generation
- [ ] Phase 6: Rate limiting, password reset, generic error responses
- [ ] Phase 7: Deploy

## Local setup

```bash
cp .env.example .env
# edit .env: set JWT_SECRET_KEY and FERNET_MASTER_KEY (see comments in .env.example
# for how to generate FERNET_MASTER_KEY)

docker compose up --build
```

Once containers are healthy:

```bash
# apply migrations
docker compose exec api alembic upgrade head

# seed fixture data (operator, 2 tenants, fixture users/docs)
docker compose exec api python -m scripts.seed
```

Check it's alive:

```bash
curl http://localhost:8000/health
```

Login as a seeded fixture user (password for all seed accounts: `ChangeMe123!`):

```bash
curl -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "admin@acme-corp.local", "password": "ChangeMe123!"}'
```

## Running the test suite

Requires a running Postgres with the `vector` extension (the `db` service from
`docker-compose.yml`, or any Postgres 16 + pgvector instance reachable via `DATABASE_URL`).

```bash
pip install -r requirements-dev.txt
alembic upgrade head
pytest -v
```

Tests run against a real database, not mocks — the RBAC query is a raw SQL comparison,
so a real DB is what actually proves it. `tests/test_rbac.py` and `tests/test_crypto.py`
use an isolated per-test transaction (rolled back at teardown, see `tests/conftest.py`);
`tests/test_auth_endpoints.py`, `tests/test_operator_endpoints.py`, and
`tests/test_admin_endpoints.py` commit their own rows and clean them up explicitly, since
they exercise the real HTTP path through the app's own DB connection.
`test_operator_endpoints.py` and `test_admin_endpoints.py` both read `app.email.dev_outbox`
directly to prove the emailed password is the real, working credential — no live SMTP
server involved (see `app/email.py`'s dev-mode fallback). `test_admin_endpoints.py`
includes one full end-to-end chain test (`test_full_chain_operator_to_admin_to_new_user`)
that goes through a real Operator login, real first-Admin creation and email, a real
`/auth/login` as that Admin, and then `/admin/users` — proving Phase 3 and Phase 3.5
actually connect, not just that each passes in isolation.

112 tests total as of Session 10 (104 from Session 9 plus 8 new in `tests/test_ingest_endpoints.py`
covering the error-handling gaps and untested surface area listed above) (91 from Phase 1–4 plus 10 new in
`tests/test_token_blacklist.py` and 1 new expiry test in `tests/test_jwt_planes.py`),
verified passing against a real Postgres 16 + pgvector instance, migrations `0001`→`0012`
applied cleanly on a fresh volume, both incrementally (against an already-migrated,
already-seeded database) and from a dropped-and-recreated one, plus a full
`alembic downgrade`/`upgrade` round-trip of every new migration including `0012`.
`tests/test_ingest_endpoints.py` overrides `app.embeddings.get_embedding_service` with a
fake, deterministic backend for the whole module — this sandbox has no network path to
huggingface.co to download real model weights, same class of constraint as the Docker
Hub / SMTP-egress gaps already noted below. The fake backend's native width (8) is
deliberately far narrower than the 1536-dim storage column, so the tests can confirm
zero-padding actually happened, not just that some vector got stored.

## Identity model

- **Operators** — global plane, no `tenant_id` anywhere. JWT never carries a `tenant_id`
  claim, so operator tokens fail decode/validation on any tenant-scoped route before the
  role gate is even reached.
- **Users** — tenant-scoped. One user = one tenant = one role (`Admin=1`, `Developer=2`,
  `Trainee=3`; lower number = higher clearance). `email` is globally unique (see
  "Known fixes" below) since `/auth/login` resolves a user by email alone.

## Core RBAC query

```sql
WHERE dv.tenant_id = :tenant_id AND dm.min_role_required >= :user_role_number
```

Centralized in `app/rbac.py::visible_document_vectors_query()` — Phase 5's `/query`
endpoint imports this rather than re-deriving the comparison. Covered by
`tests/test_rbac.py`: one fixture user per role, one fixture doc per sensitivity level,
against a real Postgres instance, including cross-tenant isolation.

## Secrets, concretely

Two static secrets matter most: `JWT_SECRET_KEY` and `FERNET_MASTER_KEY`. Per environment:

- **Local dev** — live in `.env` at the project root, which is git-ignored (see
  `.gitignore`). `.env.example` is the checked-in template with placeholder values only.
  Generate real dev values once with the commands in `.env.example`'s comments.
- **Deployed (Railway/Render)** — set as the same variable names directly in the hosting
  platform's dashboard/env var UI. Never derived from a committed file. Deployed values
  are independent secrets from local dev's — don't reuse one for the other.

`app/config.py` validates `FERNET_MASTER_KEY` is a well-formed Fernet key at process
startup (not at first encrypt call), so a misconfigured deploy — e.g. one that still has
`.env.example`'s literal placeholder string — fails loudly and immediately instead of
crashing confusingly on the first tenant that sets a BYO API key.

## Known fixes from Phase 1 (found/confirmed during Phase 2)

- `LoginRequest.email` was documented as fixed to plain `str` but had shipped as
  `EmailStr` — `email_validator` rejects `.local` as a reserved special-use domain, so
  every seeded fixture login (`admin@acme-corp.local` etc.) was actually returning 422.
  Fixed; regression-tested in `tests/test_auth_endpoints.py`.
- docker-compose `db` healthcheck was documented as fixed to include `-d guardrail_rag`
  but still lacked the flag — fixed.
- `bcrypt==4.2.0` + `passlib==1.7.4` log a spurious "(trapped) error reading bcrypt
  version" on every hash/verify call (passlib reads an attribute bcrypt 4.1+ removed).
  Harmless but noisy, same class as the healthcheck issue — pinned `bcrypt==4.0.1`.
- **New**: `users.email` had no unique constraint (plain index only), but `/auth/login`
  looks up a user by email alone and assumes exactly one match. Two same-email rows made
  login throw `MultipleResultsFound` (500) instead of authenticating. Added migration
  `0003` making `users.email` unique; regression-tested.

## Known fixes found during Phase 3 prep

- **Architecture drift, not just a naming issue**: the v2.0 correction (GuardRail owns
  no generation model of any kind — see decision log) was never actually applied to the
  shipped schema/code. `tenants.llm_provider`/`llm_model_name` were still `NOT NULL` with
  a `'groq'`/`'llama-3.1-8b-instant'` default at the DB level, `app/config.py` still had
  app-wide `default_llm_provider`/`default_groq_*` settings, and `app/crypto.py`'s
  docstrings described a "fall back to the default provider key" that doesn't exist in
  the corrected design. Fixed via migration `0004` (drops the `NOT NULL`/default),
  `app/models.py`, `app/config.py`, `.env.example`, and `app/crypto.py` doc corrections.
  Proven against a real Postgres instance: a tenant created with no model specified now
  gets `NULL`/`NULL`/`NULL`, not the old Groq default — see
  `tests/test_operator_endpoints.py::test_create_tenant_has_no_ai_model_configured`.
- `/auth/operator/login` renamed to `/operator/login` per the Design doc's corrected
  naming (decision log item #3) — removed, not aliased. Old path now 404s; regression-
  tested in `tests/test_auth_endpoints.py::test_old_auth_operator_login_path_is_gone`.
- `tenants.is_active` and `tenants.created_by_operator_id` were specified in the Design
  doc's schema but never migrated in during Phase 1/2 (no operator-plane code existed
  yet to use them). Added via migration `0005`.
- **Found after Phase 3 review**: `is_active` had the write side (`PATCH
  /operator/tenants/{id}`) but no enforcement anywhere — a deactivated tenant's users
  could still authenticate. `app/deps.py::require_user` now checks `tenant.is_active`
  against the DB on every request (not baked into the JWT, so deactivation takes effect
  immediately rather than waiting for token expiry); regression-tested in
  `tests/test_jwt_planes.py`.
- **Found after Phase 3 review**: `users.username` had no uniqueness constraint at all —
  same gap class as the `users.email` fix above, found by inspection this time rather
  than a failing test. Scoped to `(tenant_id, username)`, not global — unlike `email`,
  nothing looks a user up by `username` alone, so there's no reason two unrelated
  tenants' users should have to avoid colliding with each other. Added via migration
  `0006`; regression-tested (rejected within a tenant, allowed across tenants).

## Known fixes and additions found/made during Session 10 (ingest error handling)

Project owner asked for the three previously-flagged-but-deferred items to actually be fixed:
`app/routers/ingest.py`'s missing `DBAPIError` handling, the narrow `PdfReadError`-only catch, and
the listed untested surface area (concurrent ingests, exact file-size boundary, large documents,
embedding-call failure, missing auth header on ingest routes).

- **`DBAPIError` handling added** to all three of `_embed_and_store`, `reclassify_document`, and
  `delete_document` — same `try`/`except DBAPIError`/`rollback` shape `admin.py`/`operator.py`
  already use, via a new shared `_db_write_failed()` helper. **Uses `500 server_error`, not `400
  invalid_input`** like `admin.py`'s version — a deliberate difference, documented in the helper's
  docstring: `admin.py`'s `DBAPIError` cases are races against a known business constraint (the
  admin cap, username uniqueness), which really is "your input conflicts with something right now."
  Nothing in `ingest.py`'s writes has that shape — every constraint a document/chunk insert could
  trip is already validated before reaching the DB, so a `DBAPIError` here more likely means a
  transient infrastructure problem, and `500` is the honest classification.
- **PDF exception handling widened in two steps**: first to `pypdf`'s actual common base
  (`PyPdfError`, not just the narrower `PdfReadError` subclass — though `PdfReadError`'s own
  subclasses already covered the most likely real cases, including encrypted-without-password), then
  a broad `except Exception` fallback specifically at this one untrusted-file-parsing boundary, since
  real experimentation (truncation, corrupted `/Length`, bad object references) showed `pypdf` 5.0.1
  is quite defensive and hard to push outside its own exception family — a genuinely malformed input
  could still trip something else in principle, and this boundary is exactly the place to not let
  that leak as a raw 500.
- **Found while testing, not asked for directly, fixed anyway**: the embedding call itself
  (`embedding_service.embed_documents(...)`) wasn't wrapped in anything either — same category of
  gap as the two above. A real backend failure (OOM, a corrupted model download, a library bug)
  would have propagated raw. New `_embedding_failed()` helper, same `500 server_error` shape.
- **8 new tests**, each targeting one specific previously-named gap:
  - Missing `Authorization` header on all four ingest routes (never directly exercised before,
    though `require_role` itself is proven elsewhere).
  - A malformed PDF → safe `400`, confirming the baseline `PyPdfError` catch works.
  - A real `pypdf.PdfReader` call *monkeypatched* to raise `IndexError` (outside `PyPdfError`
    entirely) → safe `400` — this is what actually proves the broadened catch matters, since real
    malformed-PDF bytes couldn't be coaxed outside `PyPdfError`'s own hierarchy in testing.
  - `AsyncSession.commit` monkeypatched to raise `DBAPIError` → safe `500`, **and** a follow-up query
    confirming no partial `Document` row was left behind.
  - A fake embedding backend that always raises → safe `500`, same no-partial-row confirmation.
  - File size **exactly** at the configured cap (accepted) vs. one byte over (rejected) — the
    existing oversized-file test only ever checked "clearly over," not the boundary itself.
  - A ~12,000-line document → chunk count and coverage verified correct at scale. First draft of
    this test used content with no newlines at all and got exactly 1 chunk — not a bug, but a live
    demonstration of `chunking.py`'s own documented limitation (a single line longer than the target
    becomes one oversized chunk rather than being split further); fixed by using realistic multi-line
    content instead, which is what the test was actually meant to exercise.
  - Two ingests to the same tenant run concurrently (`asyncio.gather`) — both succeed independently
    with correct, non-cross-contaminated chunks.

**Verified:** full suite (112/112: 104 carried over + 8 new) run against the incrementally-migrated
database and again from a dropped-and-recreated fresh volume. No new migration this session.

## Known fixes and additions found/made during Session 9 (event loop, crash isolation, logout plane split)

Triggered by a question about whether the embedding model was ever meant to run in a separate
container (clarified: no — see decision log, item 1b) that surfaced two real, distinct concerns
worth fixing: a confirmed event-loop-blocking bug, and a legitimate crash-isolation gap.

- **`app/routers/ingest.py`'s embedding and PDF-extraction calls no longer block the event loop.**
  Both were plain blocking calls invoked directly inside `async def` functions — a real bug, not a
  hypothetical: Python's single-threaded event loop means either call froze the *entire worker* for
  its duration, stalling every other in-flight request (login, admin, another tenant's anything),
  not just the ingesting one. Fixed via `asyncio.to_thread(...)` at both call sites.
- **Production container now runs `--workers 2`** (`Dockerfile`), not 1 — addresses a real gap: a
  memory exhaustion (OOM) in `torch` during a large embed batch can get the whole process killed by
  the OS, and since auth/admin/ingestion all shared that one process, everything went down together.
  With two workers, one getting killed leaves the other serving while the process manager restarts
  the dead one. Deliberately capped at 2, not higher: each worker loads its own independent copy of
  the embedding model on first use, so worker count directly multiplies that memory cost.
  `docker-compose.yml`'s local-dev command is unchanged (`--reload`, single worker) — `--reload`
  doesn't support multiple workers, and hot-reload is worth more locally than crash isolation;
  commented in place so this isn't "fixed" into breaking local dev later.
- **Explicitly considered and not built**: a true separate embedding microservice/container, which
  neither fix above provides (they isolate *crashes*, not the *dependency surface* — a `torch`
  vulnerability in a separate container still can't reach the Fernet key or DB credentials the way
  it theoretically could in-process). Recommended against for now — meaningfully more infrastructure
  than this project has anywhere else, for a narrower threat than the OOM case `--workers 2` already
  covers. Named as a real, undecided option in the decision log, not silently dropped.
- **`POST /auth/logout` split by plane.** Session 7 built one endpoint accepting either an operator's
  or a user's token, flagged at the time as an interpretation beyond the Analysis doc's literal
  scoping. Project owner chose the stricter reading: `POST /auth/logout` is now user-plane only;
  new `POST /operator/logout` handles the operator plane. Shared blacklist-insert logic factored into
  `app/deps.py::blacklist_token()` so both routes stay identical instead of duplicating it.
- **Verified:** full suite (104/104) re-run against the incrementally-migrated database, from a
  fresh volume, and after the ingest test file's changes — no new migration this session.

## Known fixes and additions found/made during Session 7 (token blacklist)

**The gap, traced to its actual origin.** Asked directly whether "token blacklist" was
really never built — re-verified by unzipping and running the *original Phase 2
deliverable* itself, not just grepping the current codebase. Confirmed: no
`token_blacklist` table in migration `0001` (or anywhere since), no `/auth/logout` route
(`app/routers/auth.py` had exactly one endpoint, login), and no `jti` claim minted by
`_encode()` at all — so there was nothing in the token's own shape for a blacklist to
reference, even before considering whether the table existed. This was promised in
`GuardRail_RAG.md`'s own build-phase description ("Security core: ...JWT plane
separation, token blacklist") and never built, at any phase, until now.

- **`jti` added to every token** (`app/security.py::_encode()`). Tokens minted before this
  change have no `jti`; `app/deps.py::_is_blacklisted()` treats a missing `jti` as "can't
  be individually blacklisted" rather than erroring — backward compatible with any
  pre-existing token still in flight, not a special case bolted on.
- **`token_blacklist` table** (migration `0012`, `app/models.py::TokenBlacklist`) — matches
  the Design doc's schema exactly: `token_jti` alone as primary key (a `jti` is already
  globally unique), `tenant_id` nullable (operator tokens carry none).
- **`POST /auth/logout`** (`app/routers/auth.py`) blacklists the presented token's `jti`.
  Idempotent by design — logging out an already-blacklisted token, or calling logout
  twice, returns the same `204`, not an error.
- **Enforcement in both `require_user` and `require_operator`** (`app/deps.py`) — a
  blacklisted token is rejected immediately on its next request, same error shape as any
  other invalid token (`{"error": "unauthorized", "detail": "invalid or expired
  credentials"}` — no separate "this token was blacklisted" message, since that would
  only be useful information to an attacker holding it).
- **`require_operator` became async + DB-backed** to run this check (it was sync before,
  with no DB dependency at all) — same "mechanical fix" transition `require_user` went
  through in Phase 3 for its own first DB check. Two tests in `test_jwt_planes.py` that
  called it directly as a sync function were updated to `await` it with a `db_session`.
- **Scope interpretation, flagged rather than assumed:** `POST /auth/logout` accepts
  *either* plane's token, not just a tenant user's. The Analysis doc lists `/auth/logout`
  under "Auth (tenant users)" specifically, but the mechanism and the table itself
  (nullable `tenant_id`) are plane-agnostic, and there's no separate `/operator/logout`
  anywhere in the documented API surface. Read as "any token is revocable," not "only
  user tokens are" — straightforward to split into two routes later if that reading is
  wrong.
- **Also added:** the JWT-expiry test that was flagged as missing during the same audit
  (`tests/test_jwt_planes.py::test_expired_token_is_rejected`) — `decode_token()` was
  already rejecting expired tokens correctly (hand-verified before this session), it just
  had no automated test proving it, unlike every forged-token scenario next to it.

## Known fixes and additions found/made during Phase 4

**Verification pass, before writing any Phase 4 code** (per standing instruction not to
trust the decision log's claims blindly): confirmed migrations `0001`→`0008` and 74/74
tests still pass against real Postgres before building on top of this codebase — they do
(see "Running the test suite" for the fresh-volume re-verification). Then grepped the
actual shipped code (not just the decision log/design docs) for everything Phase 4 was
about to build on, and found three real gaps between what the docs described and what
was actually migrated in:

- **`project_memory` didn't exist at all.** The decision log's Session 5 addendum
  discusses this table as something to "widen... from `VECTOR(384)` to `VECTOR(1536)`" —
  language that assumes the table already existed. It doesn't, and never did: no mention
  of `project_memory` anywhere outside comments referencing *future* behavior. Migration
  `0009` creates it fresh, straight at `VECTOR(1536)`, rather than widening a narrower
  version that was never built.
- **`documents.source_type` didn't exist**, despite being in the Design doc's schema from
  the start (`document_metadata.source_type ... CHECK IN ('code','pdf','markdown')`).
  Ingestion needs this column to exist before it can record it. Added via migration `0010`
  (nullable, backfilled to `'markdown'` for the handful of pre-existing RBAC/seed fixture
  rows, then locked to `NOT NULL` + the `CHECK`).
- **`document_vectors.chunk_strategy` didn't exist**, despite the decision log discussing
  it as though it already did ("chunk_strategy is already a per-row column specifically
  so strategy can evolve later"). Added via migration `0011` (same nullable-then-backfill-
  then-`NOT NULL` pattern, backfilled to `'legacy_pre_phase4'` for pre-existing rows so a
  future reader can tell at a glance those rows predate real ingestion).

None of these three were behavior bugs — nothing was reading or depending on these
columns yet, since nothing had ingested anything before Phase 4 — but building the
ingestion pipeline directly on top of the decision log's description, without checking,
would have meant writing code against columns/tables that silently didn't exist. Found by
grepping the actual code, not by a failing test (there was no ingestion code yet for a
test to fail against).

- **Embedding-model scaffolding still matched the old HuggingFace Inference API
  framing** — `requirements.txt` had no `sentence-transformers`/`torch`, `app/config.py`
  still had `hf_api_token`/`hf_embedding_model`, and `.env.example` was labeled
  accordingly. This one *was* already flagged as a known, pending gap in the decision log
  ("Open: embedding hosting conflict") rather than a new discrepancy — propagated now:
  `app/embeddings.py` (new), `app/config.py` (`embedding_model_name`, no token needed —
  nothing leaves the process to compute an embedding), `.env.example`, `requirements.txt`.
- **`torch`'s default PyPI wheel bundles a full CUDA/cuDNN stack** (several GB of
  `nvidia-*` packages) that this project never uses — no GPU anywhere in the stated
  deploy target (Railway/Render). `requirements.txt` now pins the CPU-only build via
  `--extra-index-url https://download.pytorch.org/whl/cpu` instead. **Not verified
  installing end-to-end** in the sandbox this was built in — `download.pytorch.org` isn't
  reachable from there (same class of network restriction as this project's existing
  Docker Hub gap). Confirm with a real `pip install -r requirements.txt` or
  `docker compose build` before relying on it; the plain (non-`+cpu`) pin was confirmed to
  resolve with no version conflicts against `sentence-transformers==3.1.1`/`einops==0.8.0`
  from PyPI directly, for what that's worth on its own.
- **`documents.min_role_required`'s default changed from `3` (Trainee, most permissive) to
  `1` (Admin, strictest)** at both the ORM level (`app/models.py`) and the DB level
  (migration `0010`'s `server_default`), per the decision log's Session 5 addendum #3 —
  fail closed at creation, matching the project's existing reasoning for keeping
  reclassification Admin-only. Existing rows are untouched; this only changes what a
  future insert gets if it omits the column.
- **`documents` gets a hard delete on `DELETE /ingest/document/{id}`**, unlike
  `DELETE /admin/users`' soft deactivation. Deliberately different, not an inconsistency:
  users are soft-deleted because `project_memory.created_by_user_id`/`query_log.user_id`
  need a real row to keep resolving to ("who decided/asked what"); nothing references a
  document the same way (`document_vectors` is the only FK to it, and it's already
  `ON DELETE CASCADE`), so there's no attribution history a hard delete would break.
- **`project_memory`'s `embedding_model`/`embedding_dims` columns are an extrapolation,
  not a literal reading of the locked decision.** Session 5's addendum only named
  `embedding_dims` in its one-line summary, but its own stated reasoning ("one consistent
  embedding-storage design across both tables, not two different ones") only holds if
  this table carries `document_vectors`' full three-column pattern. Added both; flagged
  here as an extrapolation from the stated reasoning rather than a separate decision made
  silently — worth a quick confirm if that wasn't the intent.
- **Chunking is a first pass, explicitly** (decision log's own framing): fixed-size,
  approximate-token (whitespace-word-count, not the embedding model's real subword
  tokenization) chunking for all three `source_type`s, with one structural difference —
  code is chunked along line boundaries so a chunk never splits a line in half. A single
  line longer than the target chunk size (e.g. minified code) still becomes its own
  oversized chunk rather than being split further; a known, accepted limitation for now,
  not silently handled.
- **A file-size cap on `POST /ingest/document`** (`INGEST_MAX_FILE_SIZE_MB`, default 20)
  was added without being asked for. Reasoning: ingestion is synchronous this phase by
  decision (Session 5 addendum #4), so there's no background worker to hand an oversized
  upload off to — an unbounded upload ties up a request indefinitely under that model.
  Flagged explicitly per standing instruction, not folded in silently; revisit the number
  (or the sync-vs-async question itself) if real uploads need more.
- **Scope boundary, not silently decided either way:** `project_memory`'s table was
  created this phase (locked by the decision log as a Phase 4 item), but the
  `POST`/`GET /project-memory` endpoints that would actually write/read it were **not**
  built here — they aren't listed under Phase 4 in the main build-phase list (Ingestion
  covers documents, not SDLC decisions), only the schema work was explicitly locked as
  Phase 4 scope. Flagging this boundary call rather than assuming either way; happy to
  build those endpoints now if that's wanted sooner than whichever phase was intended to
  own them.
- **Also found, out of Phase 4's scope to fix, flagged rather than touched:** there is no
  `token_blacklist` table anywhere in the shipped schema, and no `/auth/logout` endpoint —
  despite Phase 2 ("Security core") being recorded as complete with "token blacklist."
  Not part of ingestion, not fixed here; noted since it turned up during this phase's
  verification pass and seems worth a look before Phase 6 (hardening) if not sooner.

## Known fixes and additions found/made during Phase 3.5

- **Documentation claim didn't match code**: both this README and the decision log
  claimed `app/crypto.py`'s docstrings were fully corrected in migration `0004`'s
  cleanup. One was missed — `DecryptionError`'s docstring still said to "fail over to
  the default provider key," a fallback that doesn't exist in the corrected
  architecture. No behavior was ever wrong (nothing in the codebase implements that
  fallback), only the comment. Fixed by re-reading the file while verifying Phase 3's
  claims before building on top of them, not by a failing test.
- **`app/user_provisioning.py::check_admin_cap_or_raise` counted all Admins, not just
  active ones.** Harmless while `users.is_active` didn't exist, but this phase adds
  `DELETE /admin/users` (deactivate) as a way to free an Admin slot — an unfixed
  pre-check would have kept rejecting a valid post-deactivation Admin creation even
  after the DB trigger would allow it. Now filters on `is_active = true`, matching the
  trigger.
- **`trg_enforce_max_admins` only fired on INSERT.** Correct when the only way to become
  an Admin was Operator-driven creation. Phase 3.5 adds a second and third way —
  `PATCH /admin/users/{id}` promoting an existing Developer/Trainee, and reactivating a
  previously-deactivated Admin — neither of which is an INSERT. Extended (migration
  `0008`) to `BEFORE INSERT OR UPDATE OF role, is_active`, counting active Admins only.
  All four transitions (create, promote, reactivate, and the "no real change" case)
  validated against a live Postgres instance before being written as a migration, not
  just reasoned about.
- **`require_user` and `/auth/login` didn't check `users.is_active`** — this column is
  new this phase, so there was nothing to check before. Added the same DB-backed,
  checked-on-every-request pattern Phase 3 already established for `tenants.is_active`,
  for the same reason: a JWT claim would mean deactivation only takes effect once a
  token expires, not immediately.
- **Two judgment calls, confirmed with the project owner after this section was
  originally written as a flag rather than a decision:**
  - `PATCH /admin/users/{id}` accepts `is_active` in both directions, so it can also
    *reactivate* a deactivated user. The Analysis doc describes how to deactivate but
    never separately describes a way back; mirroring `PATCH /operator/tenants/{id}`'s
    existing `is_active: bool | None` shape (which already supports reactivating a
    tenant) was the smallest way to close that gap. **Confirmed**, with one addition
    (below): reactivation specifically now also requires an email match, on top of
    the cap check it already had.
  - An Admin cannot target their own user id via `PATCH`/`DELETE /admin/users/{id}`.
    Not stated outright anywhere, but the Analysis doc consistently frames this endpoint
    as one Admin acting on "another"/"other" user, and without this guard a tenant's
    sole Admin could deactivate or demote themselves with no way back — Operators have
    no path into a tenant's users at all, by design. **Confirmed** as-is.
- **Reactivation now requires `confirm_email`, matching the target user's stored email
  exactly** — added after the two judgment calls above were discussed with the project
  owner. Not a security boundary (the acting Admin already sees every user's email via
  `GET /admin/users`); it's a deliberate speed bump specifically against restoring
  access for the wrong row out of a list of deactivated users — the same "type the name
  to confirm" pattern used for other high-consequence, low-frequency actions. Two other
  approaches were discussed and set aside: a fresh company/employee id was rejected,
  since it would orphan the rehired person's `project_memory`/`query_log` history under
  their old id — defeating the reason soft-deactivation exists at all; step-up password
  re-entry (a "sudo window," the pattern GitHub/AWS use) was set aside for now as
  heavier than this specific risk warrants, but is worth revisiting if token theft
  becomes an explicit threat model for this project. Deactivating needs no confirmation
  (same as `DELETE`, which was never in question) — only the false→true transition
  triggers it, checked before the admin-cap check (being sure it's the right person
  matters before checking whether there's room for them).
