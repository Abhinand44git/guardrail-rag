# ADR-0011: The retrieval endpoint returns scores and does not decide 'no answer'

- **Status:** Accepted (2026-10-25; confirmed again by the golden-set v2 run: unanswerable questions scored up to 0.713, above the lowest correct rank-1 score of 0.610)
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Depends on ADR-0002. Evidence: ADR-0007.

## Context
A cut-off on similarity score cannot reliably detect an unanswerable question with this model. In the first real-model run, correct rank-1 answers scored 0.672–0.80 (median 0.719, only 7 samples). The unanswerable questions scored 0.671 (U1) and 0.691 (U3), inside that range; only U2 at 0.590 fell below it. Nomic's scores sit in a narrow band.

## Decision
The retrieval-only endpoint returns chunks **with their scores** and leaves 'is this good enough to answer from?' to the caller's own model. GuardRail does not apply an absolute score threshold.

## Consequences
- A further point in favour of design 2 (ADR-0002).
- The response shape is not settled (blocker 4 in the handoff): query text, `project_id`, optional phase and result count in; chunk text, source type, source and score out.
- Treat the numbers above as a small sample, not a calibration.

## How this is checked
Not checkable yet. Re-run `python -m eval.run` after the owner's questions are added; the unanswerable-score check is printed there.
