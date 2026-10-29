# ADR-0008: Secrets are supplied at run time and never baked into images

- **Status:** Accepted (follow-ups open; the `hf_cache` volume mentioned below was later removed, ADR-0012)
- **Recorded:** 2026-10-15 (the date this file was written, not necessarily the date the decision was first made)
- **Related:** Fixes review Problem 4.

## Context
`.gitignore` affects only Git. Docker never reads it, so `COPY . .` copied `.env` into the image unless a separate `.dockerignore` excluded it. This was not hypothetical: the first image the owner built, before `.dockerignore` existed, really did contain `.env`, `venv`, `eval` and the compose files, and a check printed the dev `JWT_SECRET_KEY` and `FERNET_MASTER_KEY`.

## Decision
Add `.dockerignore` (`.env`, `.env.*` except `.env.example`, `.git`, `venv`, `.venv*`, caches, `eval`, compose files, `Dockerfile`). Secrets reach the container at start through compose's `env_file` (or the host's dashboard variables), never at build time. Anything present at build time is frozen into the image.

## Consequences
- After the fix (context 202 kB): no `.env`, `venv`, `eval` or compose files in `/app`, and `test -f /app/.env` printed `env-absent`; smoke script 20/20 and `pytest -q` 112 passed on the rebuilt image; the two stale `.env`-containing images were removed.
- **Use `test -f`, not `cat`, for this check** — the check itself must not print secrets.
- A `hf_cache` volume was added at the same time so the model weights are not re-downloaded when the container is recreated.
- **Open:** the two dev secrets were exposed in a chat and should be rotated (no encrypted keys exist yet, so rotation is free). Whether that was done is **unconfirmed**. Also delete the stale `HF_API_TOKEN` / `HF_EMBEDDING_MODEL` lines from the local `.env`; the app ignores them.

## How this is checked
Proven by hand (`docker compose build api`, then `docker run --rm <image> test -f /app/.env`), not by a test. A CI step running that check would make it permanent.
