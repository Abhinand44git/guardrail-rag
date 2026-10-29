# GuardRail embedding service image (ADR-0012).
# Build context is the project root (see docker-compose.yml): `docker compose build embedder`.
#
# Contract: the model weights AND the model's remote code are fetched ONCE, here, at build
# time. The running container never contacts Hugging Face. The second RUN below proves that
# by loading the model with the network switched off -- if it cannot, the build fails.
FROM python:3.12-slim

ARG MODEL_NAME=nomic-ai/nomic-embed-text-v1.5
ARG EXPECTED_DIMS=768
ARG TRUST_REMOTE_CODE=true
# warn: print any difference between revisions.lock and what was downloaded.
# strict: fail the build on any difference (recommended once the lock is filled in).
ARG REVISION_LOCK_MODE=warn

ENV EMBEDDER_MODEL_NAME=${MODEL_NAME} \
    EMBEDDER_EXPECTED_DIMS=${EXPECTED_DIMS} \
    EMBEDDER_TRUST_REMOTE_CODE=${TRUST_REMOTE_CODE} \
    REVISION_LOCK_MODE=${REVISION_LOCK_MODE} \
    HF_HOME=/opt/hf \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY embedder/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY embedder/ ./embedder/

# Step 1 (needs internet, build machine only): download weights + remote code.
RUN python -m embedder.fetch_model

# Step 2: switch the network OFF for the Hugging Face libraries and load again. A failure
# here is a failed build, not a broken container.
RUN HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m embedder.fetch_model --verify-offline

# Run as an unprivileged user. It needs write access to the cache directory only because
# transformers re-copies the model's remote code into HF_HOME/modules on load.
RUN useradd --system --uid 10001 --no-create-home embedder && chown -R embedder /opt/hf
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1
USER embedder

EXPOSE 8000

# One process on purpose: the model is held in memory once and encodes under a lock. To handle
# more load, run more replicas of this container; do not add workers (each would load its own copy).
CMD ["uvicorn", "embedder.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log"]
