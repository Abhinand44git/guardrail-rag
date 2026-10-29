"""
The real model backend for the embedder service. sentence-transformers and torch are
imported inside load(), never at module level, so importing this module (and
embedder.main) is cheap and works in environments that have neither installed -- the
API's own test suite imports embedder.main with a fake backend.
"""
from __future__ import annotations

import threading


class SentenceTransformerBackend:
    """Loads once (at service start-up), then encodes under a lock: one model instance is not
    safe to drive from several threads at once, and CPU inference gains nothing from trying."""

    def __init__(
        self,
        name: str,
        *,
        model_revision: str | None = None,
        code_revision: str | None = None,
        trust_remote_code: bool = True,
    ):
        self.name = name
        self.revision = model_revision or "unpinned"
        self.code_revision = code_revision or "unpinned"
        self.dims = 0
        self.ready = False
        self._trust_remote_code = trust_remote_code
        self._model_revision = model_revision
        self._code_revision = code_revision
        self._model = None
        self._lock = threading.Lock()

    def load(self) -> None:
        from sentence_transformers import SentenceTransformer

        kwargs: dict = {"trust_remote_code": self._trust_remote_code}
        if self._model_revision:
            kwargs["revision"] = self._model_revision
        if self._code_revision:
            # Pins the second repository that holds this model's modeling code. Both libraries
            # accept `code_revision` (sentence-transformers 3.1.1 forwards config_kwargs /
            # model_kwargs to AutoConfig / AutoModel; transformers 4.44.2 honours it).
            kwargs["config_kwargs"] = {"code_revision": self._code_revision}
            kwargs["model_kwargs"] = {"code_revision": self._code_revision}
        self._model = SentenceTransformer(self.name, **kwargs)
        self.dims = int(self._model.get_sentence_embedding_dimension())
        self.ready = True

    def encode(self, texts: list[str]) -> list[list[float]]:
        with self._lock:
            return self._model.encode(texts, convert_to_numpy=True, show_progress_bar=False).tolist()
