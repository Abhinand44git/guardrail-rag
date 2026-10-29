# ADR-0002: Phase 5 builds project memory and a retrieval-only endpoint

- **Status:** Accepted (the owner gave the go command on 2026-10-25; the blockers listed below were settled in ADR-0019 to ADR-0022)
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Depends on ADR-0001. Open questions: handoff section 5. Related: ADR-0004, ADR-0011.

## Context
With ADR-0001 in force there are two ways to answer a question. **Design 1:** GuardRail calls a tenant's model and returns the answer. **Design 2:** the user's own tool or model calls GuardRail, receives permission-filtered chunks, and writes the answer itself. Project memory (decisions saved per SDLC phase) is the project's most distinctive feature and has a table (migration `0009`) but no endpoints.

## Decision
Build Phase 5 around **project memory plus a retrieval-only endpoint (design 2)**. Design 1 stays deferred.

Why: it serves both developers and companies; it works for people on chat subscriptions and for people running a model on their own laptop; it is far smaller to build; and it keeps GuardRail's whole job on the side where the security value is — deciding which chunks a given person may see.

## Consequences
- **Chat subscriptions are not API access.** ChatGPT Plus, Claude Pro and Gemini paid plans are logins to apps, not API keys (verify per provider; plans change). Design 1 could never have used them. The README should say so.
- **A model on an employee's laptop cannot be called by a cloud server** (private addresses, routers blocking inbound calls, `localhost` meaning a different machine). It works under design 2 because the laptop calls out to GuardRail. If design 1 is ever built: drop local-model support, or allow an Admin-set reachable URL with strict SSRF checks.
- Blockers to settle before any code: who may see which memory entries (`project_memory` has no `min_role_required`; fail-closed default 1 would need a migration); whether "by SDLC phase" means the current phase only or all earlier ones too; the endpoint's name and shape; where `project_id` comes from; updating the four docs that still promise generation.
- Out of this phase: MCP wrapper, provider adapters, `user_llm_config`, `PATCH /admin/tenant-config`, Admin failure notifications.

## How this is checked
Not checkable yet. `test_project_memory_shape` is the tripwire for the memory-visibility decision: it fails the day `min_role_required` is added to `project_memory`.
