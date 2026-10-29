# GuardRail RAG — Role-Based Multi-Tenant Knowledge Engine

**Status:** Planned / in progress — built alongside the Kompetenzen AI-Assisted Full Stack Python course
**Version:** 2.0 (corrected) · August 2026
**Supersedes:** v1.0 (May 2026) — this revision corrects the core idea itself; see "What changed in v2.0" at the bottom.

## What it is
A security layer that sits between a company's private documents/codebases and whatever AI model employees already use for work. It does **not** provide its own AI model. Companies already have AI access — a paid subscription, a local model on an employee's laptop, an API key. GuardRail's job is to make it safe to use that AI on private company data, by never letting the AI see more than small, permission-checked fragments of it — and to remember project decisions across the SDLC instead of losing that context every session.

## Core problem it solves
Employees already paste private company docs and code into AI tools like Gemini, Claude, or ChatGPT to get useful answers. That's a direct leak: the raw private material goes straight into a third party's model. On top of that, a consultant working across multiple clients needs a system that:
- Never sends a whole document or codebase to an external AI model — only small, relevant, pre-filtered fragments
- Adapts its knowledge base per client (multi-tenancy)
- Enforces who can access what within a client's org, before anything reaches the AI (RBAC)
- Lets each client bring the AI model they already pay for or already run locally — GuardRail is not itself an AI provider
- Remembers decisions made across the full SDLC — design through production
- Amplifies developer speed while keeping a human in control via mandatory review

## Architecture — three layers
| Layer | What it does |
|---|---|
| **LLM Engine** | Not owned by GuardRail. Each tenant configures which AI model answers queries — a company subscription (Admin-set default) or an individual's own paid model (personal override). GuardRail only ever calls whichever model is configured; it never ships or defaults to one of its own. |
| **RAG Layer** | Retrieves the small set of relevant chunks from the client's knowledge base via vector search — this is what stands between the full private dataset and the AI model. The model only ever sees these chunks, never the source documents. |
| **Security Layer** | Enforces tenant isolation and role-based access *before* any chunk is retrieved, let alone sent to a model. |

## Tech stack
- **Backend:** FastAPI (Python)
- **Database:** PostgreSQL + pgvector
- **Dev hosting:** Supabase free tier (documented future option: AWS RDS with customer-managed keys, or fully self-hosted — see Decision Log)
- **LLM generation:** none owned by GuardRail. Every tenant must configure a provider — Admin sets the company default; Developer/Trainee/Admin may each personally override with their own key
- **Embeddings:** nomic-embed-text-v1.5, self-hosted in-process via sentence-transformers
  (768-dim, zero-padded to the shared 1536-dim storage width — see Design doc §2). Not the
  HuggingFace Inference API: sending every ingested chunk to a third party in plaintext was
  judged a larger, ungated exposure than the generation-model question this doc's BYO-model
  correction was originally about (100% of ingested content, not just what a query's
  RBAC/similarity filter surfaces, and not gated by any Admin configuration step the way
  generation is). Originally specced as bge-small-en-v1.5 over the Inference API; corrected
  during Development, Phase 4 — see Decision Log.
- **Auth:** JWT + bcrypt (passlib)
- **Containerization:** Docker + docker-compose

## Key design decisions
- **Security filter before LLM:** access control enforced at the DB query level, not by prompting the LLM to behave. The LLM never receives data the user isn't authorized to see — regardless of which AI model is configured.
- **No GuardRail-owned AI model:** GuardRail is a filter and memory layer, not an AI provider. Every tenant must configure at least a company-default model before `/query` becomes usable.
- **Strict tenant isolation:** every table scoped by `tenant_id`; no query can cross tenant boundaries.
- **Role hierarchy:** lower role number = higher clearance (1 = admin, 3 = trainee). Enforced with a single query joining `document_vectors` → `document_metadata`, filtering on `tenant_id` and `role <= min_role_required`. This filter runs identically no matter which AI model is configured — model choice and document clearance are separate concerns.
- **Portability:** switching clients = switching `ACTIVE_TENANT_ID`, not rebuilding the system.
- **Model-change resilience:** `embedding_model` and `chunk_strategy` are stored per row in `document_vectors`, so upgrading the embedding model doesn't require dropping the whole table.
- **SDLC-aware project memory:** decisions saved during design are retrievable during coding/production — a dual RAG retrieval (company knowledge + project memory) scoped by tenant, project, and SDLC phase.

## Core database tables
`operators` · `tenants` · `roles` · `users` · `user_llm_config` · `document_metadata` · `document_vectors` · `project_memory` · `token_blacklist`

## Scope boundaries
**In scope:** multi-tenant RAG with pgvector, RBAC, JWT auth, SDLC phase-aware memory, document ingestion (code/PDF/markdown), admin dashboard endpoints, per-tenant and per-user AI model configuration (BYO key), Docker local deployment, seed script for demo data.

**Out of scope:** GuardRail hosting or defaulting to its own AI model, fine-tuning any LLM, autonomous code deployment, real-time collaboration, mobile/desktop client, third-party IDE plugin, billing, production cloud hosting, non-text file support.

## Build phases (see Decision Log for full detail and current status)
1. **Foundation:** FastAPI + pgvector setup, tenants/roles/users + JWT, code ingestion pipeline, `/query` endpoint skeleton, seed data
2. **Security core:** full RBAC enforcement, JWT plane separation, token blacklist
3. **Operator endpoints:** tenant creation, first Admin creation (password via email), `max_admins` management
3.5. **Admin user management:** Admin creates Developer/Trainee accounts
4. **Ingestion:** document upload, chunking, embedding, deletion/sensitivity updates (Admin-only)
5. **Query/retrieval + generation:** RBAC-filtered vector search, context budget, tenant/user-configured model call
6. **Hardening:** rate limiting, generic errors, full password reset, CORS/HTTPS
7. **Deploy + polish:** Supabase (or later, AWS RDS/self-hosted), secrets set, README

## Working method
Built via a triangle of AI (Claude/Cursor) for code generation, the Kompetenzen course for FastAPI/PostgreSQL/auth fundamentals, and the developer for human review and architectural correctness — every AI-generated change reviewed against a fixed checklist (tenant isolation, role logic direction, SQL injection risk, JWT/role-change handling, safe error messages) before commit.

## Market context
This is not a novel category — enterprise RAG-over-codebase assistants (Sourcegraph Cody, Augment Code, Tabnine Enterprise, GitHub Copilot Enterprise) and document-level RBAC RAG patterns are established 2026 enterprise practice. The value of this project as a portfolio piece is demonstrating hands-on understanding of *why* those systems are architected this way (security-first, tenant-isolated, versioned embeddings, no unnecessary vendor exposure) — the SDLC-phase-aware project memory and the BYO-model philosophy are the more distinctive design contributions.

## What makes it stand out
- Security is architectural, enforced at the DB layer before any AI model is ever called
- GuardRail deliberately does not own or default to an AI model — it's infrastructure, not a vendor
- Multi-tenancy is first-class, not bolted on
- Embedding model versioned per row — shows awareness of real production migration concerns
- Docker setup verifiable in under 5 minutes

---

## What changed in v2.0 (corrected from v1.0)
The original handout assumed GuardRail would ship with its own default generation model (first Groq, later reversed to a self-hosted model) and let tenants optionally override it. That was a misreading of the original intent: **GuardRail was never meant to provide an AI model at all.** Companies already have one — local or API-key-based — and GuardRail's actual job is only to keep private data safe *regardless* of which model is used. This revision removes the GuardRail-owned-default concept entirely, replacing it with a required, tenant-configured default (set by the Admin) plus optional per-user overrides (Developer/Trainee/Admin). Full reasoning and decision trail in the Decision Log.
