"""
Embedding generation for ingestion and (Phase 5) queries.

Self-hosted, and since ADR-0012 in its own container. The model does not run inside this
API process: a separate `embedder` service holds the weights and answers `POST /embed`.
This module is the API's side of that conversation. Why a separate container:
  - Privacy: sending every ingested chunk to a third party's servers was judged a larger,
    ungated exposure than the generation-model question (100% of ingested content, not just
    what a query's RBAC/similarity filter surfaces) -- so nothing here ever calls a hosted
    embedding API. The embedder image has the model baked in and no route to the internet.
  - No per-worker model copies, no ~20 s first-request download/load on whichever user
    happens to ingest first, and the API image no longer carries torch.

Model: settings.embedding_model_name (nomic-ai/nomic-embed-text-v1.5 by default), native 768
dims. That name must match the model the embedder image was built with: every response
carries the model's name and HttpEmbeddingBackend refuses a mismatch, because
document_vectors.embedding_model is what retrieval filters on and must never be a guess.

The model expects distinct "search_document: " / "search_query: " text prefixes depending on
which side of retrieval a piece of text is on -- dropping or swapping these doesn't error, it
just quietly returns worse matches. Both live on EmbeddingService below, next to each other,
so ingestion-time and query-time embedding can never drift apart. The embedder itself is a
plain text->vector service and knows nothing about prefixes.

Width: document_vectors.embedding and project_memory.embedding are VECTOR(768), exactly this
model's native width -- no padding (migration 0013, ADR-0019, which superseded ADR-0014's
zero-padding to 1536). A vector of any other width is REFUSED, never padded or cut: a model
with a different width needs a migration and a re-embed, and a silent adjustment here would
store vectors that cannot be compared with the rest. embedding_dims still records the width
the model reported, so a future model change stays visible in the data.

Testability: EmbeddingBackend is a Protocol, so most tests substitute a small deterministic
fake. The HTTP path itself is tested against a real, live embedder app running a fake model
(tests/test_embedder_service.py). The real model is exercised by scripts/smoke_real_model.py
inside the compose stack.
"""
from __future__ import annotations

import logging
import threading
from typing import Protocol

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# document_vectors.embedding / project_memory.embedding column width (migration 0013).
STORAGE_DIMS = 768


class EmbedderError(Exception):
    """The embedder answered, but not in a usable way (rejected the request, malformed reply,
    or serves a different model than GuardRail is configured for)."""


class EmbedderUnavailable(EmbedderError):
    """The embedder could not be reached, timed out, or is not ready (no model loaded yet).
    Retrying later may well succeed."""


class EmbeddingBackend(Protocol):
    """What EmbeddingService needs from a model backend."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        ...

    @property
    def native_dims(self) -> int:
        ...


class HttpEmbeddingBackend:
    """
    Talks to the embedder service. Synchronous on purpose: the ingest route already runs
    embedding on a worker thread (asyncio.to_thread), and httpx.Client is thread-safe.

    Large inputs are sent in batches of `batch_size`, one request each, so a 100-chunk upload
    is never a single multi-minute request. Request text is never logged here.
    """

    def __init__(
        self,
        base_url: str,
        *,
        expected_model: str,
        timeout: float = 120.0,
        batch_size: int = 16,
        client: httpx.Client | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._expected_model = expected_model
        self._batch_size = max(1, batch_size)
        self._client = client or httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0))
        self._dims: int | None = None

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            response = self._client.request(method, self._base_url + path, **kwargs)
        except httpx.TransportError as e:  # connect failure, timeout, dropped connection, ...
            logger.warning("embedder unreachable: %s", type(e).__name__)
            raise EmbedderUnavailable(f"embedder unreachable ({type(e).__name__})") from e
        if response.status_code in (502, 503, 504):
            raise EmbedderUnavailable(f"embedder not ready (HTTP {response.status_code})")
        if response.status_code != 200:
            raise EmbedderError(f"embedder rejected the request (HTTP {response.status_code})")
        try:
            return response.json()
        except ValueError as e:
            raise EmbedderError("embedder returned a response that is not JSON") from e

    def _check_model(self, served: object) -> None:
        if served != self._expected_model:
            raise EmbedderError(
                f"the embedder serves {served!r} but GuardRail is configured for "
                f"{self._expected_model!r}: rebuild the embedder image or fix EMBEDDING_MODEL_NAME"
            )

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            batch = texts[i : i + self._batch_size]
            data = self._request("POST", "/embed", json={"texts": batch})
            self._check_model(data.get("model"))
            got, dims = data.get("vectors"), data.get("dims")
            if not isinstance(got, list) or len(got) != len(batch) or not isinstance(dims, int):
                raise EmbedderError("embedder returned a malformed embedding response")
            if any(len(v) != dims for v in got):
                raise EmbedderError("embedder returned vectors that do not match its own reported dims")
            self._dims = dims
            vectors.extend(got)
        return vectors

    @property
    def native_dims(self) -> int:
        if self._dims is None:
            info = self._request("GET", "/info")
            self._check_model(info.get("model"))
            self._dims = int(info["dims"])
        return self._dims


def require_storage_width(vector: list[float], expected: int = STORAGE_DIMS) -> list[float]:
    """Return the vector unchanged if it is exactly the storage width, otherwise refuse.
    (Replaces zero_pad(): ADR-0019. Refusing beats guessing -- see the module docstring.)"""
    if len(vector) != expected:
        raise ValueError(
            f"embedding has {len(vector)} dims but the storage columns are {expected} wide: "
            "a model with a different width needs a migration and a re-embed, "
            "not a silent pad or cut"
        )
    return list(vector)


class EmbeddingService:
    """
    The one thing ingestion and retrieval call. Wraps a
    backend with GuardRail's storage conventions: the correct nomic prefix
    per side of retrieval, a check that the width matches storage, and returning
    embedding_model/embedding_dims alongside each vector so callers can
    write all three document_vectors/project_memory columns without
    re-deriving any of this themselves.
    """

    DOCUMENT_PREFIX = "search_document: "
    QUERY_PREFIX = "search_query: "

    def __init__(self, backend: EmbeddingBackend, model_name: str):
        self._backend = backend
        self._model_name = model_name

    def embed_documents(self, texts: list[str]) -> list[dict]:
        """
        Ingestion-side embedding, one call for a whole batch of chunks (one
        model invocation, not one per chunk). Returns one dict per input
        text, in order: {"embedding": [...768 floats...],
        "embedding_model": str, "embedding_dims": int}.
        """
        if not texts:
            return []
        prefixed = [self.DOCUMENT_PREFIX + t for t in texts]
        raw = self._backend.encode(prefixed)
        dims = self._backend.native_dims
        return [
            {"embedding": require_storage_width(vec), "embedding_model": self._model_name, "embedding_dims": dims}
            for vec in raw
        ]

    def embed_query(self, text: str) -> dict:
        """
        Query-side embedding (used by POST /retrieve). Kept here, right next to
        embed_documents, specifically so the search_query: / search_document:
        prefixes can't silently drift apart from each other over time.
        """
        raw = self._backend.encode([self.QUERY_PREFIX + text])
        dims = self._backend.native_dims
        return {"embedding": require_storage_width(raw[0]), "embedding_model": self._model_name, "embedding_dims": dims}


_default_service: EmbeddingService | None = None
_default_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """
    FastAPI dependency. A singleton so the HTTP client (and its connection pool) is created
    once per process. Tests override this dependency with a fake backend (see
    tests/test_ingest_endpoints.py), so a test run never needs an embedder.
    """
    global _default_service
    if _default_service is None:
        with _default_lock:
            if _default_service is None:
                backend = HttpEmbeddingBackend(
                    settings.embedder_url,
                    expected_model=settings.embedding_model_name,
                    timeout=settings.embedder_timeout_seconds,
                    batch_size=settings.embedder_batch_size,
                )
                _default_service = EmbeddingService(backend, settings.embedding_model_name)
    return _default_service
