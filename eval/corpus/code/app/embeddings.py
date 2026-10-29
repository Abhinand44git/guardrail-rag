"""
Embedding generation for ingestion (Phase 4) — self-hosted, not the
HuggingFace Inference API. See the decision log's "Open: embedding hosting
conflict" section: sending every ingested chunk to a third party's servers
was judged a larger, ungated exposure than the generation-model question the
original BYO-model correction was about (100% of ingested content, not just
what a query's RBAC/similarity filter surfaces, and not gated by any Admin
configuration step) — so embeddings are computed in-process instead.

Model: settings.embedding_model_name (nomic-ai/nomic-embed-text-v1.5 by
default), native 768 dims, loaded via sentence-transformers with
trust_remote_code=True (this model ships custom modeling code alongside its
weights, not just weights). It also expects distinct "search_document: " /
"search_query: " text prefixes depending on which side of retrieval a piece
of text is on — dropping or swapping these doesn't error, it just quietly
returns worse matches (the decision log's own reminder). Both prefixes live
on EmbeddingService below, next to each other, specifically so ingestion-time
and query-time embedding can never drift out of sync with one another.

Zero-padding: document_vectors.embedding and project_memory.embedding are
both VECTOR(1536) for model-change-resilience. This model's native output is
narrower (768), so every vector is zero-padded out to 1536 before storage;
embedding_dims records the true, pre-padding dimensionality so a later
migration to a wider/narrower model doesn't have to guess it back out of a
vector full of trailing zeros.

Testability note: this sandbox (and most CI environments) has no network
path to huggingface.co to actually download model weights — same class of
constraint as this project's existing Docker Hub / SMTP-egress workarounds.
EmbeddingBackend is a Protocol specifically so tests can substitute a fake,
deterministic backend (see tests/test_ingest_endpoints.py) instead of
requiring a real model download to exercise the ingestion pipeline. The real
backend (SentenceTransformerBackend) is written for production use but was
not exercised end-to-end against real model weights in this environment —
flagged explicitly, not glossed over, per this project's own convention for
sandbox limitations.
"""
from __future__ import annotations

import threading
from typing import Protocol

from app.config import settings

# document_vectors.embedding / project_memory.embedding column width.
STORAGE_DIMS = 1536


class EmbeddingBackend(Protocol):
    """What EmbeddingService needs from a model backend."""

    def encode(self, texts: list[str]) -> list[list[float]]:
        ...

    @property
    def native_dims(self) -> int:
        ...


class SentenceTransformerBackend:
    """
    Real backend. Lazily loads the model on first use (not at import time or
    process startup) so importing this module never triggers a model
    download — only actually calling the embedding service does. Loaded
    once per process, guarded by a lock so two concurrent first-requests
    don't each try to load their own copy.
    """

    def __init__(self, model_name: str):
        self._model_name = model_name
        self._model = None
        self._lock = threading.Lock()

    def _ensure_loaded(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    # Imported here, not at module level: sentence-transformers
                    # (and torch underneath it) are real, heavy dependencies
                    # that only the actual embedding call needs — importing
                    # app.embeddings elsewhere (e.g. app.main at startup)
                    # shouldn't require them to be importable yet.
                    from sentence_transformers import SentenceTransformer

                    self._model = SentenceTransformer(self._model_name, trust_remote_code=True)

    def encode(self, texts: list[str]) -> list[list[float]]:
        self._ensure_loaded()
        return self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False).tolist()

    @property
    def native_dims(self) -> int:
        self._ensure_loaded()
        return self._model.get_sentence_embedding_dimension()


def zero_pad(vector: list[float], target_dims: int = STORAGE_DIMS) -> list[float]:
    if len(vector) > target_dims:
        raise ValueError(
            f"embedding has {len(vector)} dims, wider than the {target_dims}-dim "
            "storage column — this model needs document_vectors/project_memory "
            "widened before it can be used, not silently truncated"
        )
    return list(vector) + [0.0] * (target_dims - len(vector))


class EmbeddingService:
    """
    The one thing ingestion (and, later, Phase 5's /query) calls. Wraps a
    backend with GuardRail's storage conventions: the correct nomic prefix
    per side of retrieval, zero-padding to the storage width, and returning
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
        text, in order: {"embedding": [...1536 floats...],
        "embedding_model": str, "embedding_dims": int}.
        """
        if not texts:
            return []
        prefixed = [self.DOCUMENT_PREFIX + t for t in texts]
        raw = self._backend.encode(prefixed)
        dims = self._backend.native_dims
        return [
            {"embedding": zero_pad(vec), "embedding_model": self._model_name, "embedding_dims": dims}
            for vec in raw
        ]

    def embed_query(self, text: str) -> dict:
        """
        Query-side embedding (Phase 5). Kept here, right next to
        embed_documents, specifically so the search_query: / search_document:
        prefixes can't silently drift apart from each other over time.
        """
        raw = self._backend.encode([self.QUERY_PREFIX + text])
        dims = self._backend.native_dims
        return {"embedding": zero_pad(raw[0]), "embedding_model": self._model_name, "embedding_dims": dims}


_default_service: EmbeddingService | None = None
_default_lock = threading.Lock()


def get_embedding_service() -> EmbeddingService:
    """
    FastAPI dependency. Singleton so the model loads once per process, not
    once per request — loading it is the expensive part, encoding a batch is
    cheap by comparison. Tests override this dependency with a fake backend
    (see tests/test_ingest_endpoints.py) rather than exercising this
    function, so a test run never triggers a real model load.
    """
    global _default_service
    if _default_service is None:
        with _default_lock:
            if _default_service is None:
                backend = SentenceTransformerBackend(settings.embedding_model_name)
                _default_service = EmbeddingService(backend, settings.embedding_model_name)
    return _default_service
