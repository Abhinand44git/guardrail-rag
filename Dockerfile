FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --workers 2, not 1: a crash in one worker process takes down only that worker -- the other
# keeps serving login/admin/everything else while the process manager restarts the dead one.
# Workers are cheap now: the embedding model lives in the separate `embedder` container
# (ADR-0012), so this image carries no torch and a second worker costs little memory. (Before
# that split each worker loaded its own copy of the model, which is why this was capped at 2.)
# This CMD is the production path (what Railway/Render actually run); local dev's
# docker-compose.yml overrides it with --reload instead, see that file's comment for why the
# two don't mix.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
