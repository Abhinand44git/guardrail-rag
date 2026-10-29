"""
GuardRail embedding service (ADR-0012).

A deliberately small, separate process whose only job is turning text into vectors. The
model weights AND the model's remote code are baked into this container's image at build
time, so at run time it never contacts Hugging Face (and in docker-compose it sits on a
network with no route to the internet at all).

Privacy: this service receives raw document chunks and search queries -- the content
GuardRail exists to protect. It therefore (a) is never published to the host, (b) never
logs request text, and (c) never echoes request text back, not even in validation errors.

Run:  uvicorn embedder.main:create_app --factory
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.requests import Request

DEFAULT_MAX_BATCH = 32
MAX_TEXT_CHARS = 200_000  # per text; a 500-word chunk is a few thousand characters


class Backend(Protocol):
    name: str
    revision: str
    dims: int
    ready: bool

    def load(self) -> None: ...
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class EmbedRequest(BaseModel):
    texts: list[str]


def _backend_from_env() -> Backend:
    from embedder.backend import SentenceTransformerBackend
    from embedder.revisions import load_lock, revisions_for

    name = os.environ["EMBEDDER_MODEL_NAME"]
    lock_path = Path(__file__).with_name("revisions.lock")
    model_revision, code_revision = revisions_for(load_lock(lock_path), name)
    return SentenceTransformerBackend(
        name,
        model_revision=model_revision,
        code_revision=code_revision,
        trust_remote_code=os.environ.get("EMBEDDER_TRUST_REMOTE_CODE", "true").lower() == "true",
    )


def create_app(backend: Backend | None = None, *, max_batch: int | None = None) -> FastAPI:
    """App factory. `backend` is injectable so tests (and anything without torch) can run the
    real HTTP layer against a fake model; with None, the real model is loaded from the image."""
    limit = max_batch or int(os.environ.get("EMBEDDER_MAX_BATCH", DEFAULT_MAX_BATCH))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        be = backend if backend is not None else _backend_from_env()
        if not be.ready:
            be.load()  # blocking by design: the container is not healthy until the model is in memory
        app.state.backend = be
        yield

    app = FastAPI(title="GuardRail embedder", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    # One request encodes at a time: a single model instance, CPU-bound. Scale with replicas.
    encode_lock = threading.Lock()

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # FastAPI's default 422 body echoes the offending input back. Here that input is
        # confidential document text, so say only that the body was invalid.
        return JSONResponse(status_code=422, content={"detail": "invalid request body"})

    def _backend() -> Backend:
        be = getattr(app.state, "backend", None)
        if be is None or not be.ready:
            raise HTTPException(status_code=503, detail="model not loaded")
        return be

    @app.get("/health")
    def health():
        be = _backend()
        return {"status": "ok", "model": be.name}

    @app.get("/info")
    def info():
        be = _backend()
        return {"model": be.name, "revision": be.revision, "dims": be.dims, "max_batch": limit}

    @app.post("/embed")
    def embed(req: EmbedRequest):
        be = _backend()
        if not 1 <= len(req.texts) <= limit:
            raise HTTPException(status_code=422, detail=f"batch must contain 1 to {limit} texts")
        if any(len(t) > MAX_TEXT_CHARS for t in req.texts):
            raise HTTPException(status_code=422, detail=f"each text must be at most {MAX_TEXT_CHARS} characters")
        with encode_lock:
            vectors = be.encode(req.texts)
        if len(vectors) != len(req.texts):
            raise HTTPException(status_code=500, detail="backend returned the wrong number of vectors")
        return {"model": be.name, "revision": be.revision, "dims": be.dims, "vectors": vectors}

    return app
