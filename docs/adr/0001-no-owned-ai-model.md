# ADR-0001: GuardRail owns no AI model and holds no AI keys

- **Status:** Accepted (reaffirmed)
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Supersedes the Groq and Granite entries in the Decision Log (kept there as history). Defers 'design 1' (see ADR-0002).

## Context
The generation model was reversed twice: first a GuardRail default of Groq (`llama-3.1-8b-instant`), then self-hosted IBM Granite via Ollama, then neither. Each default created a vendor-exposure question the product never needed to ask. The *schema and code* lagged the *docs* by a whole phase (migration `0004` finally removed the `'groq'` default), which is one of the cases behind Problem 3.

## Decision
GuardRail never owns, hosts or defaults to a generation model, and never holds an AI key of its own.

Hosted generation — a `/query` that calls a model, `user_llm_config`, `PATCH /admin/tenant-config`, provider adapters, Admin key-failure notifications — is **deferred ("design 1"), not dropped**. Embeddings are a separate matter: they are computed in-process by a self-hosted model (ADR-0005).

## Consequences
- `tenants.llm_provider`, `llm_model_name`, `llm_api_key_ref` exist, are nullable and have no default. Nothing reads them yet.
- `user_llm_config` and `query_log` do not exist.
- The Analysis, Design and README docs, and Phase 5 in the Decision Log, still promise `/query` with generation. They are stale until ADR-0002 is settled.
- The project prompt still says "Groq (generation)" and "HuggingFace Inference API (embeddings)". Both are wrong and should be edited.

## How this is checked
`tests/test_schema.py`: `test_tenant_llm_columns_are_nullable_with_no_default`, `test_new_tenant_has_no_model_configured`, and the table-set tripwire (`test_table_set_matches_the_documented_schema`), which fails if `user_llm_config` or `query_log` appears without a new ADR.
