# ADR-0021: The retrieval and memory endpoints read the caller's role from the database, not the token

- **Status:** Accepted; its open question about the older endpoints was closed by ADR-0024
- **Recorded:** 2026-10-25
- **Related:** ADR-0013, ADR-0017.

## Context
`require_user` returns the JWT's claims, including `role`. A role claim is frozen at login: if an Admin demotes someone, their token keeps the old clearance until it expires (60 minutes). For a role *gate* that is a bounded problem; for the value that decides **which stored text a person may read**, it is not acceptable.

## Decision
`/retrieve` and `/project-memory` use `deps.require_live_user` / `require_live_role`, which take the tenant and role from the user's database row (already loaded in the same request, so no extra query) and refuse a token whose tenant does not match the user's own. Nothing in a request body can steer either value.

## Consequences
- A demotion takes effect on the person's very next retrieval, not when their token expires.
- **Not changed, flagged:** `/ingest/*` and `/admin/*` still gate on the token's role. A demoted Admin could keep Admin powers there for up to 60 minutes. The fix is small (have `require_user` overwrite the claim with the database value) but changes existing behaviour, so it waits for the owner.

## How this is checked
`test_a_token_that_claims_admin_but_whose_user_is_now_a_trainee_gets_trainee_results`, `test_demoting_a_user_takes_effect_on_their_very_next_retrieve_not_when_their_token_expires`, `test_a_token_whose_tenant_does_not_match_the_users_own_tenant_is_refused`, and the memory-endpoint equivalents. Mutation-checked.
