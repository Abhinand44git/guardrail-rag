# ADR-0003: Portfolio framing: state scope limits honestly, don't build around them

- **Status:** Accepted
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Audience: mixed developers and companies.

## Context
GuardRail is a portfolio project, not a product for large companies. Its value is demonstrating architectural understanding. Building every enterprise feature would be impossible and would hide the decisions that matter.

## Decision
Where a limit is deliberate, state it plainly in the README (and in an ADR) instead of working around it. Known examples: no per-team access scoping (ADR-0004), one Fernet key with no rotation, no login rate limiting until Phase 6, Supabase now with customer-managed keys documented as a future path.

## Consequences
A limit written in the README is a promise to readers; if the README is silent the limit looks like an oversight. The README sections for the limits above are still to be written.

## How this is checked
Not checkable by a test. Reviewed by hand when the README is updated.
