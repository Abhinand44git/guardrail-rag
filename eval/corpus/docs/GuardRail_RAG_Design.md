# GuardRail RAG — Design

**Phase:** Design (SDLC step 3 of 5 — Planning → Analysis → **Design** → Development → Testing/Deployment)
**Builds on:** GuardRail_RAG_Analysis.md v2.0 (corrected)
**Stack:** FastAPI · PostgreSQL + pgvector (Supabase, near-term) · JWT/bcrypt · Docker Compose · self-hosted nomic-embed-text-v1.5 via sentence-transformers for embeddings (corrected from the original HuggingFace Inference API / BAAI/bge-small-en-v1.5 spec during Development, Phase 4 — see Decision Log's "Open: embedding hosting conflict") · no GuardRail-owned generation model · Railway/Render deploy

---

## 1. Request flow

**Diagram A — Auth + RBAC gate (unchanged):** Client request (JWT) → decode + validate (checks `token_blacklist`) → role gate (`role <= min_role_required`) → build scoped SQL filter (`tenant_id` + role).

**Diagram B — Dual RAG retrieval (unchanged):** Scoped filter → pgvector similarity search → merge company-knowledge + project-memory chunks → trim to 4,000-token budget → generation call, using whichever model resolves for the requester (see Diagram C).

**Diagram C — Model resolution (new):**
1. Look up `user_llm_config` for the requesting user.
2. If a row exists → use that provider/model/key.
3. If no row exists → fall back to `tenants.llm_provider`/`llm_model_name`/`llm_api_key_ref`.
4. If **neither** is set (tenant has no company default and user has no personal override) → `/query` returns a safe error telling the user their organization's AI model isn't configured yet, and to contact their Admin. This is a real, expected state right after a tenant is created and before an Admin runs `PATCH /admin/tenant-config`.
5. If the resolved key fails at call time → see §3 below for the fallback/notification contract.

**Operator hard-block (structural, unchanged):** an operator JWT carries no `tenant_id` claim, so it fails decode+validate before any role, document, or model-resolution logic runs. Operators have no path to any part of Diagram C — they never resolve or touch model config for any tenant.

## 2. Database schema

```sql
-- Operators: separate plane, no tenant_id anywhere in this table or its FKs pointing outward from it
CREATE TABLE operators (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE tenants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    max_admins INT NOT NULL DEFAULT 1,
    created_by_operator_id UUID REFERENCES operators(id),
    is_active BOOLEAN DEFAULT true,
    -- Company-default generation model. Deliberately NO DEFAULT VALUE on any of these three columns:
    -- GuardRail does not ship or fall back to an AI model of its own. A tenant is created with these
    -- null, and /query is blocked for that tenant until an Admin sets them via PATCH /admin/tenant-config.
    llm_provider TEXT,
    llm_model_name TEXT,
    llm_api_key_ref TEXT,  -- reference to an encrypted secret, NEVER a raw key in this column
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Enforces max_admins at the DB layer, not just in application code.
CREATE OR REPLACE FUNCTION enforce_max_admins() RETURNS TRIGGER AS $$
DECLARE
    admin_count INT;
    cap INT;
BEGIN
    SELECT max_admins INTO cap FROM tenants WHERE id = NEW.tenant_id;
    SELECT count(*) INTO admin_count
    FROM users u JOIN roles r ON r.id = u.role_id
    WHERE u.tenant_id = NEW.tenant_id AND r.role_number = 1;
    IF (SELECT r.role_number FROM roles r WHERE r.id = NEW.role_id) = 1 AND admin_count >= cap THEN
        RAISE EXCEPTION 'tenant % already has the maximum of % admins', NEW.tenant_id, cap;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_enforce_max_admins BEFORE INSERT ON users
FOR EACH ROW EXECUTE FUNCTION enforce_max_admins();

CREATE TABLE roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    role_number INT NOT NULL CHECK (role_number BETWEEN 1 AND 3),
    role_name TEXT NOT NULL,
    UNIQUE (tenant_id, role_number)
);

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role_id UUID NOT NULL REFERENCES roles(id),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (tenant_id, email)
);
CREATE INDEX idx_users_tenant ON users(tenant_id);

-- New table: personal AI model overrides. One row per user who has opted in.
-- No row = "use the tenant's company default." Kept separate from `users` rather than
-- adding nullable columns there, for two reasons: (1) it isolates a security-sensitive
-- column (an encrypted API key reference) away from a table that's queried constantly
-- for unrelated reasons (login, listing users, admin dashboards); (2) it leaves room to
-- later support more than one saved config per user without a schema migration.
CREATE TABLE user_llm_config (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id),
    tenant_id UUID NOT NULL REFERENCES tenants(id),  -- denormalized, same rationale as document_vectors.tenant_id below
    llm_provider TEXT NOT NULL,
    llm_model_name TEXT NOT NULL,
    llm_api_key_ref TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_user_llm_config_tenant ON user_llm_config(tenant_id);

CREATE TABLE document_metadata (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    title TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK (source_type IN ('code','pdf','markdown')),
    min_role_required INT NOT NULL CHECK (min_role_required BETWEEN 1 AND 3),
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_docmeta_tenant ON document_metadata(tenant_id);

CREATE TABLE document_vectors (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES document_metadata(id),
    tenant_id UUID NOT NULL REFERENCES tenants(id),  -- denormalized on purpose: lets the RBAC filter query this table directly with a single WHERE, no join required on the hot path
    chunk_text TEXT NOT NULL,
    embedding VECTOR(1536),
    embedding_model TEXT NOT NULL,
    embedding_dims INT NOT NULL,
    chunk_strategy TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_vectors_tenant ON document_vectors(tenant_id);
CREATE INDEX idx_vectors_embedding ON document_vectors USING ivfflat (embedding vector_cosine_ops);

CREATE TABLE project_memory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    project_id UUID NOT NULL,
    sdlc_phase TEXT NOT NULL CHECK (sdlc_phase IN ('planning','analysis','design','development','testing','deployment')),
    content TEXT NOT NULL,
    created_by_user_id UUID NOT NULL REFERENCES users(id),
    embedding VECTOR(384),
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_memory_tenant_project ON project_memory(tenant_id, project_id);

CREATE TABLE query_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    user_id UUID NOT NULL REFERENCES users(id),
    project_id UUID,
    query_text TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_querylog_tenant_user ON query_log(tenant_id, user_id);

CREATE TABLE token_blacklist (
    token_jti TEXT PRIMARY KEY,
    tenant_id UUID REFERENCES tenants(id),
    blacklisted_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
```

**Design note on `document_vectors.tenant_id` and `user_llm_config.tenant_id`:** both are deliberately denormalized for the same reason — each supports a high-frequency, security-relevant lookup (the RBAC filter; model resolution) where a join would add an avoidable hop on a hot path. A single indexed `WHERE tenant_id = $1 AND ...` beats joining out through a parent table in both cases.

**Design note on the two swap points (unchanged distinction, corrected ownership):** (1) Generation model — resolved per-request via the `user_llm_config` → `tenants` fallback chain in Diagram C. GuardRail never writes to or owns this call; it only decides *which* configured endpoint to call. (2) Embedding model — `document_vectors.embedding` widened to `VECTOR(1536)` with zero-padding for smaller models, `embedding_dims` tracking true dimension, `embedding_model` a required filter on every retrieval query.

## 3. Model config write contract (new — governs both `PATCH /admin/tenant-config` and `PATCH /user/llm-config`)

Both endpoints share one rule: **validate before saving, all-or-nothing.**

1. Request arrives with `llm_provider`, `llm_model_name`, `llm_api_key_ref` (raw key, not yet encrypted at this point).
2. Backend makes a single, minimal, non-generative test call to the named provider (e.g. a lightweight "list models" or equivalent ping) — never a real prompt, no reason to spend tokens validating a key.
3. **If the test fails:** reject the entire request. Nothing is written — not the provider name, not the model name, nothing partial. Response uses the standard safe-error contract (§5), e.g. `{"error": "invalid_input", "detail": "This API key could not be verified. Please check it and try again."}` — never the raw provider error text.
4. **If the test succeeds:** encrypt the key (Fernet, see below) and write all three fields together in one transaction.

This applies identically whether it's an Admin setting the tenant's company default or a user (any of the three tenant roles) setting their own personal override — one rule, no special case per role.

**Query-time failure (distinct from save-time validation):** a key can still fail later even if it validated at save time (expired, revoked externally, rate-limited). At query time:
- If the **tenant's company-default** key fails and the querying user has no personal override → the query fails for that user with a safe error, **and** the Admin is notified immediately (not just logged for later) — a broken company default blocks every user who doesn't have their own override, so this needs to surface promptly, not be discovered accidentally.
- If a **user's own personal override** key fails → that user gets a safe, explicit error/warning naming the problem in general terms (e.g. "your configured AI model key isn't working — check it or contact your Admin"). No Admin notification needed here; it only affects that one user.

## 4. `/query` response contract (new)

Every `/query` response includes:
```json
{
  "answer": "...",
  "model_used": "provider/model-name",
  "note": null
}
```
`note` is populated (not left null) whenever something worth telling the user just happened:
- A model switch was requested and saved: `"You're now using <model> for future queries too, not just this one."`
- A personal key failed and the system could not proceed for that user: surfaced as an error response instead of a normal answer (see §3).

The exact wording is judged per situation at build time — this contract fixes the *shape* (a field the caller can always check), not a rigid fixed string.

## 5. Docker Compose layout (unchanged)

```yaml
services:
  api:
    build: ./api          # FastAPI app
    env_file: .env
    ports: ["8000:8000"]
    depends_on: [db]

  db:
    image: supabase/postgres  # or ankane/pgvector for pure local dev
    volumes: ["pgdata:/var/lib/postgresql/data"]
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD}

volumes:
  pgdata:
```

Two services remain enough. There is no model-serving container of any kind — GuardRail never hosts a generation model, so the earlier open question about an Ollama/`llama-cpp-python` third container is moot and removed from scope entirely (see Decision Log for the full reversal history).

## 6. Error-message contract (unchanged, restated)

Every 4xx/5xx from `api` returns:
```json
{"error": "not_found" | "unauthorized" | "invalid_input" | "server_error", "detail": "<generic, tenant-agnostic message>"}
```
Never include: tenant IDs, role numbers, table/column names, raw exception text, or raw AI-provider error text. Log the real detail server-side with a request ID; return the request ID to the client so you can correlate without leaking anything in the response body itself.

## 7. What's deliberately not designed here

No custom role creation, no full audit log (only the lightweight derived last-modified indicator described in the Decision Log), no multi-tenant user membership, no GuardRail-hosted AI model of any kind, no automatic model-provider failover.

---

**Next:** Development, Phase 1 (Foundation) — this schema as Alembic/SQL migrations (note: `tenants.llm_*` columns ship nullable with no default; new `user_llm_config` and `query_log` tables added), JWT issuing for both planes, seed script for 1 operator + 2 demo tenants (seeded *without* AI model config, to exercise the "not configured yet" path deliberately).
