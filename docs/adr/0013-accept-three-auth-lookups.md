# ADR-0013: Accept the per-request authentication lookups; optimise only after measuring

- **Status:** Accepted
- **Recorded:** 2026-10-18
- **Related:** Review item 10. Related: ADR-0018.

## Context
Every authenticated request checks, in the database, that the tenant is active, the user is active, and the token is not blacklisted. That is what makes revocation immediate: deactivating a tenant or user, or logging out, takes effect on the next request rather than when the token expires. It costs a few indexed primary-key lookups per request.

## Decision
Keep it as is. No caching, no restructuring, until a measurement says it matters.

When it does (revisit triggers: authenticated-endpoint latency in which these lookups are a measurable share, or sustained request rates where the database connection pool is the constraint), the first step is collapsing the lookups into a single query, which keeps revocation immediate. Caching with a time-to-live is **rejected**: it would make revocation lag by up to the TTL, and differently on each worker.

## Consequences
- The price of immediate revocation is paid on every request; at demo scale it is milliseconds.
- No latency measurement exists yet. Taking one would be the first step of any optimisation.
- Whoever optimises this later is touching `app/deps.py`, the most security-sensitive file: it needs the full test suite and a security review, not a quick change.

## How this is checked
Not a code change. The property any future optimisation must preserve is guarded by existing end-to-end tests: `tests/test_token_blacklist.py` (a logged-out token is rejected on the very next request), `tests/test_admin_endpoints.py` and `tests/test_operator_endpoints.py` (deactivated users and tenants are rejected immediately), and `tests/test_jwt_planes.py`.
