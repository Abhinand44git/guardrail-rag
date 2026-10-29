# ADR-0024: Every authenticated endpoint uses the caller's current role from the database

- **Status:** Accepted (closes the open question in ADR-0021)
- **Recorded:** 2026-10-25
- **Related:** ADR-0013, ADR-0017, ADR-0021.

## Context
ADR-0021 made the Phase 5 endpoints read the role from the database and left the older endpoints (`/ingest/*`, `/admin/*`) on the role frozen into the login token, where a demoted Admin kept Admin powers for up to `jwt_expire_minutes` (60). The owner asked for that to be fixed without a database change and without a patch that would only work in tests.

## Decision
`app/deps.py::require_user` now returns the token's claims **with `role` replaced by the role on the user's database row**, and refuses a token whose `tenant_id` differs from the user's own tenant. The user row was already loaded by `require_user` for the `is_active` check, so this adds **no query and no schema change**. The token's role claim is now informational only.

Why this is not a fragile override: `require_role` was the only place in `app/` that read the role from the token (verified by search), and it reads it only from `require_user`'s return value. So there is one source of truth and no second path that still trusts the token. The Phase 5 helpers `require_live_user` / `require_live_role` are now thin typed wrappers over it, not a separate lookup.

## Consequences
- A demotion or a promotion takes effect on the person's very next request, on every endpoint.
- The token's role claim is still minted at login (harmless; kept so existing tokens and tooling keep working).
- Unchanged: the Operator plane (`require_operator`), the logout dependencies (decode-only on purpose), tenant/user deactivation checks, blacklist checks.
- Costs nothing extra per request. ADR-0013's accepted "three lookups per authenticated request" still describes the database work.

## How this is checked
`tests/test_role_is_read_from_the_database.py`: a token claiming Admin for a Trainee is refused on `/admin/*`; a demoted Admin loses `/admin/*` on the next request, including when another Admin demotes them through the real `PATCH /admin/users/{id}`; a demoted Developer and a demoted Admin lose `/ingest/*`; a promotion works immediately; a token/user tenant mismatch is refused; a deactivated user is still refused. Mutation-checked: putting back `return payload` fails six of them, removing the tenant check fails the seventh.
