# ADR-0012: The embedding model runs in its own container, baked into the image

- **Status:** Accepted (fully verified on the owner's machine, 2026-10-18: image build, healthy start, pinned model and code commits confirmed with the network disabled, no route to the internet, `pytest` 213 passed and 2 skipped, smoke test 20/20, a stopped embedder gives a clean 503 after about 4 s, idle memory about 0.9 GB)
- **Recorded:** 2026-10-18
- **Related:** Supersedes the runtime-download and per-process-loading consequences of ADR-0005. Builds on ADR-0001 (no hosted AI), ADR-0006 (pins). Resolves review item 12.

## Context
Until now the model ran inside the API process: lazily loaded on the first request, once per worker, downloaded from Hugging Face into a cache volume, with `trust_remote_code=True` fetching modeling code from a second repository at whatever commit it happened to be at. That meant a ~20 s first-request penalty for whoever ingested first, a model copy per worker (which is why the API was capped at 2 workers), a runtime dependency on huggingface.co, and an unpinned third-party code path inside the process that handles every document.

The owner's requirement, recorded as non-negotiable: the model lives in a **separate container that already contains it**, instead of the system reaching out to Hugging Face.

## Decision
A small `embedder` service (`embedder/`) owns the model; the API holds an HTTP client (`HttpEmbeddingBackend`) and nothing else.

- **Baked at build time.** `embedder/Dockerfile` downloads the weights *and* the model's remote-code repository once, during `docker build` (the only moment internet access is needed, on the build machine). A second build step then loads the model with `HF_HUB_OFFLINE=1`; if that fails, **the build fails**, so there is never an image that works only while Hugging Face is reachable.
- **Pinned.** `embedder/revisions.lock` fixes both repositories to full commit hashes: `nomic-ai/nomic-embed-text-v1.5` at `e9b6763…` and its remote code `nomic-ai/nomic-bert-2048` at `7710840…` — the two commits recorded by the real run in `eval/results/`. Both are passed to the loader (`revision` and `code_revision`, which the pinned library versions accept). `REVISION_LOCK_MODE=strict` makes any later difference fail the build.
- **No route out.** In compose the embedder is on an `internal: true` network shared only with the API: no internet, no published port. At run time it cannot contact Hugging Face even if something tried.
- **Privacy.** The service sees raw chunks and queries. It never logs request text and never echoes it, not even in validation errors (tested).
- **Contract.** `POST /embed {texts}` returns `{model, revision, dims, vectors}`; `GET /health` is healthy only once the model is loaded; the API waits for it (`depends_on: service_healthy`). Prefixes and zero-padding stay in the API (`EmbeddingService`), so the embedder is a plain text-to-vector service.
- **Model identity is checked.** Every response names the model; the API refuses to store vectors from a model other than `EMBEDDING_MODEL_NAME`, because `embedding_model` is what retrieval filters on.

## Consequences
- The API image no longer contains torch (a large reduction), starts instantly, and can run several workers cheaply. The `hf_cache` volume and the API's Hugging Face settings are gone. The old volume can be deleted: `docker volume rm <project>_hf_cache`.
- **Speed is unchanged.** Embedding is still CPU-bound, about 3 s per chunk on a laptop. Separation does not make ingestion faster; asynchronous ingestion remains an open item. The API sends 16 chunks per request with a 120 s timeout (both settings).
- **An unavailable embedder returns 503** ("try again shortly") and writes nothing; any other embedding failure is still a generic 500.
- One embedder process per container, encoding under a lock. Scale with replicas, not workers.
- **To poke the embedder** (it has no host port): `docker compose exec api python -c "import httpx; print(httpx.get('http://embedder:8000/info').json())"`.
- **Memory:** the model plus torch must fit Docker Desktop's allocation; the container is capped at 3 GB by default (`EMBEDDER_MEM_LIMIT`). Not yet measured.
- **Hosting:** a deployment target needs a second always-on service with 1–2 GB of RAM. If that is ever impossible, the interface (`EmbeddingBackend`) allows an in-process backend again; that would be a new ADR.
- **Changing the model** means changing `EMBEDDING_MODEL_NAME`, `revisions.lock`, rebuilding the embedder, and re-embedding stored vectors (see ADR-0014).
- `eval/` is unaffected: it still runs in its own virtual environment with its own copy of the libraries.

**Not yet verified (needs Docker and internet on your machine):** the embedder image build itself; offline loading inside it; the real model answering over HTTP; the `code_revision` pin working in practice (confirmed from the libraries' source, not by running); memory use. Checklist below.

## How this is checked
**Verified here, with a real HTTP server and a fake model:** 22 tests in `tests/test_embedder_service.py` (batching and order, timeouts and refused connections raise the retryable error, model-name mismatch is refused, requests are serialised, text is never echoed or logged, ingest answers 503 and writes nothing, the API image has no ML libraries, compose isolates the embedder from the internet and the host, the Dockerfile verifies offline loading and runs unprivileged), plus 12 in `tests/test_embedder_revisions.py` for the lock file. Each was checked by deliberately breaking the code it guards.

**Checklist for your machine:**
```
docker compose build embedder      # needs internet, once. Expect "ok: loaded and encoded (768 dims)" and then the same
                                   # line with "with the network disabled". Read any WARNING lines.
docker compose up -d && docker compose ps          # embedder (healthy), then api
docker compose exec embedder python -c "import urllib.request as u; u.urlopen('https://huggingface.co', timeout=5)"
                                   # must FAIL: the embedder has no route to the internet
docker compose exec api pytest -q                  # expect 2 skipped (compose/Dockerfile checks are not inside the API image)
docker compose exec api python -m scripts.smoke_real_model       # the real model, through HTTP
docker compose stop embedder   # then ingest a file: expect HTTP 503, not 500
```
If the build fails at the first `fetch_model` step, send me the log: the likeliest causes are the CPU torch index being unreachable or the `code_revision` keyword not being accepted. Once the lock matches, set `REVISION_LOCK_MODE=strict` (build arg) so drift fails the build.
