FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# --workers 2, not 1: a crash/OOM in one worker process (e.g. torch running
# out of memory during a large embed batch) takes down only that worker --
# the other keeps serving login/admin/everything else while the process
# manager restarts the dead one. Deliberately not higher than 2: each worker
# loads its own independent copy of the embedding model into memory on
# first use (app/embeddings.py's singleton is per-process, not shared
# across workers), so worker count directly multiplies that memory cost --
# tune upward only alongside the container's memory limit, not by itself.
# This CMD is the production path (what Railway/Render actually run);
# local dev's docker-compose.yml overrides this with --reload instead, see
# that file's comment for why the two don't mix.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
