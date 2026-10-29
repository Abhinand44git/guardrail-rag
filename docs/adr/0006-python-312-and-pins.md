# ADR-0006: Develop and deploy on Python 3.12 with a verified dependency pin set

- **Status:** Accepted (the torch, transformers, sentence-transformers and einops pins now live in `embedder/requirements.txt`, ADR-0012; the Python version applies to both images)
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Related: ADR-0005.

## Context
On the owner's machine the project `venv` was Python 3.13.2, where `torch==2.4.1+cpu` has no wheel, so `pip` could not find it. That was first misdiagnosed as PyTorch having pruned old versions, and the pin was briefly changed to `2.14.1+cpu`. That change was wrong and was reverted. Separately, with `transformers` 5.x and `sentence-transformers` 6.x, `nomic-embed-text-v1.5`'s downloaded model code fails with `AttributeError: 'NomicBertModel' object has no attribute 'get_extended_attention_mask'`.

## Decision
- Develop and deploy on **Python 3.12**, matching the Dockerfile (`python:3.12-slim`). On Windows install **3.12.10**: it is the last 3.12 release with binary installers.
- Pins: `torch==2.4.1+cpu`, `sentence-transformers==3.1.1`, `transformers==4.44.2`, `einops==0.8.0`, `pypdf==5.0.1`.
- Python 3.10 reaches end-of-life in October 2026, and 3.13 is unsupported by this pin set (no `torch==2.4.1` wheel).

## Consequences
- Only the project's own packages are pinned; `numpy`, `scipy` and `scikit-learn` resolve to latest at install time. `pip freeze > requirements.lock` is a suggested, not-yet-done step.
- The pins were verified on the owner's machine (Python 3.12.10; the model loads and returns shape `(1, 768)`) and inside the Docker image (`pytest -q`: 112 passed, per the handoff).

## How this is checked
Not enforced by a test. Verified by hand as described above; re-verify on any dependency change with `docker compose exec api python -m scripts.smoke_real_model`.
