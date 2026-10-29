# ADR-0017: A person belongs to exactly one tenant; consultants use one address per client

- **Status:** Accepted
- **Recorded:** 2026-10-18
- **Related:** Related: ADR-0003, ADR-0004.

## Context
`users.email` is unique across all tenants and login finds a user by email alone, so one email can only ever mean one account in one tenant. A consultant working for several client companies therefore cannot hold one login across them.

## Decision
Keep it, and document it as a known limit. The workaround is one address per client, for example `pat+clienta@example.com` and `pat+clientb@example.com`, which many mail providers deliver to one inbox (check yours supports it).

Rejected for now: multi-tenant membership (a membership table, a tenant choice at login, email no longer globally unique). That rewrites login and the identity model, which is security-critical, and nothing in the project's goals needs it.

## Consequences
- The README states the limit.
- If real demand appears, the work is: a `memberships` table, tenant selection at login, and a token that carries the chosen tenant. The RBAC query itself would not change.

## How this is checked
`tests/test_schema.py::test_email_is_unique_across_all_tenants` pins the rule; `tests/test_api_surface.py::test_plus_addressed_emails_let_one_person_hold_separate_accounts_in_different_tenants` proves the workaround is accepted end to end.
