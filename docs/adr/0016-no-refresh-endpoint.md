# ADR-0016: There is no /auth/refresh endpoint; sessions last 60 minutes

- **Status:** Accepted
- **Recorded:** 2026-10-18
- **Related:** Review item 15. Related: ADR-0013.

## Context
`POST /auth/refresh` is listed in the Analysis doc (line 46) and described in the Decision Log as part of the "documented API surface". It was never built. Every saved snapshot of the project (Phases 1, 3, 3.5, 4 and the current tree) was searched: no route, test or migration mentions it; the only code hits for "refresh" are SQLAlchemy's `db.refresh(obj)`. What *was* built, in Phase 4, is logout and the token blacklist; that is a different feature and is easy to confuse with it.

## Decision
Do not build refresh tokens. Access tokens last `jwt_expire_minutes` (60); afterwards the user signs in again. Correct the Analysis doc and state this in the README.

Rejected: re-issuing a new token from a still-valid one (a stolen token could then renew itself forever); proper refresh tokens (a server-side table, rotation, reuse detection, logout covering both). The second is the right design if long sessions are ever needed, but it is a security-critical batch of its own.

## Consequences
- **Doc to correct:** `GuardRail_RAG_Analysis.md` line 46, remove `POST /auth/refresh`. The Decision Log mention is history and stays.
- Users re-authenticate hourly, which is acceptable for a demo and for a security-first tool.

## How this is checked
`tests/test_api_surface.py`: the route table must equal the documented list exactly, and `test_there_is_no_refresh_endpoint` fails if a refresh path appears, prompting a new ADR first.
