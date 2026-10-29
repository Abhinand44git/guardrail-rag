# ADR-0018: The token blacklist stays in PostgreSQL; expired rows are purged on logout

- **Status:** Accepted
- **Recorded:** 2026-10-18
- **Related:** Review item 11. Related: ADR-0013. Future: login rate limiting (Phase 6).

## Context
Expired `token_blacklist` rows were never removed. They are harmless to correctness (an expired token is rejected by its own `exp` claim before the blacklist is consulted), but the table only ever grew. A cron job would be one more thing to deploy and forget. Redis, with a time-to-live per key, is the textbook design for this and was considered.

## Decision
Keep the blacklist in PostgreSQL, and on every logout delete up to 500 already-expired rows (`purge_expired_blacklist`, using the existing `expires_at` index). No cron, no scheduler, no new service. The purge runs *after* the revocation is committed and is best-effort: a failure is logged and never fails or undoes the logout.

Redis is **not adopted now**, for three reasons: a restart of an unpersisted Redis would make logged-out tokens valid again for up to 60 minutes; its outage forces a fail-open (revoked tokens accepted) or fail-closed (nobody can authenticate) choice that Postgres does not have; and it is one more service for a table that stays tiny, since rows live at most 60 minutes.

## Consequences
- In steady state the table holds only the last hour's logouts. If nobody logs out for a long time, old rows wait until the next logout, which then drains them in batches.
- **Redis becomes worth adding with login rate limiting (Phase 6),** where shared counters with expiry are exactly its job. If it arrives for that, moving the blacklist there is cheap, and that move would get its own ADR and a persistence decision.
- Existing test hygiene note: the operator-logout tests leave one blacklist row per run (their cleanup filters by tenant, and operator tokens have none). Harmless; expired rows are now purged anyway.

## How this is checked
`tests/test_token_blacklist.py`: expired rows are removed and live ones kept, the purge is bounded per call, a logout purges and still revokes its own token, and a failing purge never fails the logout. Each was checked by breaking the purge three ways (deleting nothing, deleting everything, not catching errors).
