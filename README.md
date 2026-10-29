# GuardRail RAG

**A multi-tenant, role-gated retrieval layer for private code and documents.** Permission filtering happens in the database query, before any AI model sees a single chunk.

`FastAPI` · `PostgreSQL + pgvector` · `JWT + bcrypt` · `self-hosted embedding service` · `Docker Compose`

> **Status: a portfolio project, in progress.** Ingestion, authentication, tenancy and access control are built and tested. Retrieval (`POST /retrieve`) and project memory (Phase 5) are built and tested; answer generation is deliberately not part of GuardRail. See [What works today](#what-works-today) and [Known limits](#known-limits).

## The idea

Employees paste private code and documents into AI tools. That is a leak. GuardRail sits between a company's private material and whatever AI model its people already use. It owns **no AI model and no AI keys**. Its one job is to make sure that a model only ever receives small, permission-filtered fragments, never whole documents, and never anything the asking person isn't cleared to see.

The design bet is that the security rule should live in one place, in the database query, where it can be tested against a real database:

```sql
WHERE dv.tenant_id = :tenant_id AND dm.min_role_required >= :user_role_number
```

(Lower role number = higher clearance. See [`app/rbac.py`](app/rbac.py) and [`tests/test_rbac.py`](tests/test_rbac.py).)

## How it is put together

```
 client ──HTTP──> api ──────────> db        PostgreSQL + pgvector
                   │
                   └──(private network, no internet)──> embedder   the model, baked into its image
```

Three containers. The **embedder** holds the embedding model and its code, downloaded and pinned once at *build* time; the running container never contacts Hugging Face and sits on an `internal` Docker network with no route to the internet or the host. The API reaches it by name and does not contain the model at all ([ADR-0012](docs/adr/0012-separate-embedder-container.md)).

## How the security model works

- **Tenant isolation and clearance in one query.** Every chunk read goes through `visible_document_vectors_query()`. Tests run it against a real Postgres + pgvector instance: Admin sees all, Developer and Trainee see less, and no role ever sees another tenant's data.
- **Two separate identity planes.** *Operators* (who create tenants) have no `tenant_id` and cannot touch any tenant data. *Users* (Admin / Developer / Trainee) are tenant-scoped. The planes use different JWT claims, and each rejects the other's tokens.
- **Fail closed.** A newly uploaded document is Admin-only until an Admin loosens it. A tenant has no AI model until configured, and there is no default vendor.
- **Revocation takes effect immediately.** Tenant and user `is_active` flags and a token blacklist are checked in the database on every request, not baked into the token.
- **Admin cap enforced by the database.** A trigger on `users` stops a tenant exceeding `max_admins`, including via promotion or reactivation.
- **Soft deletion.** Users are deactivated, not erased, so project history keeps pointing at a real person.
- **Secrets.** Static secrets come from environment variables at run time and are never baked into the image (`.dockerignore`); per-row API keys are Fernet-encrypted. Key rotation is out of scope.

## What works today

| Area | Endpoints |
|---|---|
| Auth (users) | `POST /auth/login`, `POST /auth/logout` |
| Operator | `POST /operator/login`, `POST /operator/logout`, `POST /operator/tenants`, `PATCH /operator/tenants/{id}`, `POST /operator/tenants/{id}/admin` |
| Admin | `POST` / `GET /admin/users`, `PATCH` / `DELETE /admin/users/{id}` |
| Ingestion | `POST /ingest/code`, `POST /ingest/document` (PDF, Markdown), `PATCH` / `DELETE /ingest/document/{id}` |
| Project memory | `POST` / `GET /project-memory` (decisions tagged with an SDLC phase; new entries are Admin-only unless the writer sets a looser level) |
| Retrieval | `POST /retrieve`: the document chunks and memory entries **you are allowed to see** that are closest in meaning to a question, with similarity scores |

**How retrieval works.** `POST /retrieve` takes `{"query": "...", "k": 5}` (optionally `project_id` and `phase`) and returns two ranked lists, `documents` and `memory`, each hit with a cosine `score`. Which rows can appear is decided by the caller's tenant and clearance (read from the database, not the login token) and, for memory, by authorship, *before* anything is ranked; `project_id` and `phase` only narrow results and can never widen them ([ADR-0020](docs/adr/0020-memory-visibility-by-clearance.md), [ADR-0023](docs/adr/0023-authors-see-their-own-memory.md), [ADR-0024](docs/adr/0024-role-from-database-everywhere.md), [ADR-0022](docs/adr/0022-retrieve-contract.md)). GuardRail never calls a language model: your own tool or model takes these fragments and writes the answer.

**Not built yet:** answer generation (by design), reclassifying or deleting memory entries, password reset and login rate limiting (Phase 6), and deployment (Phase 7).

## Quick start

Requires Docker and a few GB of free disk (the embedder image includes PyTorch and the model). Building it needs internet **once**.

```bash
git clone https://github.com/Abhinand44git/guardrail-rag.git
cd guardrail-rag

cp .env.example .env        # PowerShell: Copy-Item .env.example .env
# Edit .env and set JWT_SECRET_KEY and FERNET_MASTER_KEY. To generate them:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker compose up --build -d      # first build downloads PyTorch (CPU) and the ~550 MB model; later builds are cached
docker compose exec api alembic upgrade head        # migrations first, then seed
docker compose exec api python -m scripts.seed

curl http://localhost:8000/health
```

Interactive API docs are at `http://localhost:8000/docs`. Log in as a seeded fixture user (`admin@acme-corp.local`, password `ChangeMe123!`) at `POST /auth/login`. The seed accounts are test fixtures: never seed a deployed database.

**Cost.** The model is downloaded once, while building the `embedder` image, so nothing is fetched at run time and the first upload is not slow to start. Embedding itself runs on CPU at roughly 3 seconds per chunk on a laptop, so a large upload is still slow; ingestion is synchronous by design for now. If the embedder is down, uploads fail with a clear `503` and save nothing. The embedder container is capped at 3 GB by default (`EMBEDDER_MEM_LIMIT`); make sure Docker itself has that much available. Measured idle use is about 0.9 GB (memory during a very large upload has not been measured).

## Tests

```bash
docker compose exec api pytest -q        # 322 tests; expect 319 to pass and 3 to skip inside the container (they check the compose and embedder Dockerfile, which are not copied into the API image)
```

Tests run against a real Postgres + pgvector, not mocks. Most test behaviour end to end over HTTP. [`tests/test_schema.py`](tests/test_schema.py) pins what the documentation claims about the database (constraints, defaults, the admin-cap trigger) and compares `models.py` against the migrations; [`tests/test_api_surface.py`](tests/test_api_surface.py) pins the exact list of endpoints; [`tests/test_embedder_service.py`](tests/test_embedder_service.py) runs the embedder's real HTTP layer on a real socket against a stand-in model. The tests were checked by deliberately breaking the code they guard, to confirm they fail when they should.

To check the real model end to end (API, embedder over HTTP, database): `docker compose exec api python -m scripts.smoke_real_model`.

## Does retrieval actually work? An evaluation harness

Passing tests proves correctness, not usefulness, so [`eval/`](eval/) holds a hand-written golden question set over a frozen snapshot of this project's own docs and code, scored with recall@k and MRR against a BM25 keyword baseline.

```bash
python -m eval.run --model nomic-ai/nomic-embed-text-v1.5 --k 10
```

Add `--dims 768,512,256,128` to also score the same vectors cut to those widths (one embedding pass); this is how the 768-wide storage decision was made ([ADR-0019](docs/adr/0019-vectors-768-no-padding.md)).

First results on the original 26 questions (golden set v1, mostly written by the AI assistant that helped build the project; small sample, read with care): at k=5, recall rose from 43% (BM25) to 61% (embeddings); at k=10 they tie at 65%, so the embeddings' real advantage is ranking the right chunk higher. Each method finds answers the other misses, which is the case for hybrid search, **not yet decided**. The set has since grown to 35 questions, with five rewritten and nine added by the project owner in their own words (`eval/golden_set.json`; the original is kept as `golden_set_v1.json`); the model has not been re-scored on it yet. Details and caveats: [ADR-0007](docs/adr/0007-eval-harness.md).

## Known limits

Stated plainly, on purpose ([ADR-0003](docs/adr/0003-portfolio-framing.md)):

- **No per-team scoping.** Access is controlled by tenant and clearance level only. `project_id` is a client-supplied label, **not a security boundary** ([ADR-0004](docs/adr/0004-no-projects-tables.md)).
- **One user, one tenant.** A consultant working for several clients uses one address per client, such as `pat+clienta@example.com` ([ADR-0017](docs/adr/0017-one-user-one-tenant.md)).
- **Sessions last 60 minutes, then you sign in again.** There are no refresh tokens ([ADR-0016](docs/adr/0016-no-refresh-endpoint.md)).
- **Dense-only search, no vector index.** Searches will be exact scans; fine at demo scale.
- **Slow synchronous ingestion** and **fixed-size chunking**, sized in words (about 1.9 model tokens per word on this project's own text), not in model tokens ([ADR-0015](docs/adr/0015-words-not-tokens.md)).
- **The embedding model runs third-party modeling code** (`trust_remote_code=True`). It is pinned to exact commits in `embedder/revisions.lock` and fetched only at build time ([ADR-0012](docs/adr/0012-separate-embedder-container.md)), but it is still code you are choosing to trust.
- **Vectors are stored at exactly the model's 768 numbers.** A model with a different width needs a migration and a re-embed ([ADR-0019](docs/adr/0019-vectors-768-no-padding.md)).
- **Memory entries cannot yet be reclassified or deleted** once saved. You always see the ones you wrote; everyone else's follow clearance ([ADR-0020](docs/adr/0020-memory-visibility-by-clearance.md), [ADR-0023](docs/adr/0023-authors-see-their-own-memory.md)).
- **Chat subscriptions are not API access.** ChatGPT Plus, Claude Pro and Gemini plans are logins to apps, not API keys, which is why GuardRail is retrieval-only ([ADR-0002](docs/adr/0002-phase-5-retrieval-only.md)).
- Real SMTP is unverified (only the dev outbox is exercised); one Fernet key, no rotation; no login rate limiting yet.

## Project layout

```
app/            FastAPI app: routers, RBAC query (rbac.py), auth (security.py, deps.py), embeddings client, chunking
embedder/       The embedding service: its own image, pinned model commits (revisions.lock), build-time offline check
alembic/        Database migrations (the source of truth for the schema)
tests/          Behaviour tests + schema contract tests
eval/           Retrieval evaluation harness, golden set, frozen corpus, saved results
scripts/        seed.py, smoke_real_model.py
docs/           CURRENT_STATE.md, adr/ (decisions), BUILD_LOG.md (history)
```

## Troubleshooting

- **`ports are not available: ... 127.0.0.1:5432 ... forbidden by its access permissions` (Windows).** Another PostgreSQL on your machine (often a Windows service) already owns port 5432. Add `DB_HOST_PORT=5433` to your `.env` and run `docker compose up -d` again. Only tools on your own machine use that port (`localhost:5433`); the API reaches the database inside Docker and is unaffected.
- **An old database volume is reused after you rename or re-clone the project folder.** Docker names volumes after the folder, so a stale one can hold an old schema and old demo data. `docker compose down -v` deletes this project's volume; then `up -d`, `alembic upgrade head`, `python -m scripts.seed`.
- **`pytest: executable file not found` right after `docker compose up -d`.** The api container installs the test tools on start; wait until `docker compose logs api` shows "Application startup complete".

## Documentation

- [`docs/CURRENT_STATE.md`](docs/CURRENT_STATE.md): what exists, what doesn't, how it was last verified
- [`docs/adr/`](docs/adr/README.md): one short file per architectural decision
- [`docs/BUILD_LOG.md`](docs/BUILD_LOG.md): session-by-session build history

## License

No license has been chosen yet, so by default all rights are reserved. Add a `LICENSE` file (for example MIT) if you want others to reuse the code.
