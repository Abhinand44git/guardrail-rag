"""
The separate embedder service (ADR-0012) and the API's client for it.

What is real here: the embedder's own HTTP app (embedder/main.py) is run on a genuine socket
by uvicorn in a background thread, and the API's HttpEmbeddingBackend talks to it over actual
HTTP -- so connection-refused, timeouts, 503s, batching and concurrency are the real behaviours,
not mocks of them. What is fake: the model (a tiny deterministic stand-in), because the real one
needs ~550 MB of weights and a network path to Hugging Face. The real model is covered by
scripts/smoke_real_model.py inside the compose stack.

Privacy is tested explicitly: the embedder handles raw document text, so a sentinel string must
never come back in an error body or appear in a log record.
"""
import contextlib
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.database import AsyncSessionLocal
from app.embeddings import (
    EmbedderError,
    EmbedderUnavailable,
    EmbeddingService,
    HttpEmbeddingBackend,
    get_embedding_service,
)
from app.main import app as api_app
from app.models import Document, Role
from embedder.main import create_app
from tests.test_ingest_endpoints import _cleanup, _make_client, _make_tenant, _make_user, _token

ROOT = Path(__file__).resolve().parents[1]
MODEL = "fake-embedder-model"
SENTINEL = "TOP-SECRET-CHUNK-9f3a"


# --------------------------------------------------------------------------
# a tiny fake model + a way to run the real embedder app on a real port
# --------------------------------------------------------------------------
class FakeBackend:
    """Deterministic stand-in for the model. Records what it was asked, and how concurrently."""

    revision = "fake-rev"
    dims = 8

    def __init__(self, *, name: str = MODEL, delay: float = 0.0, stuck: bool = False):
        self.name = name
        self.delay = delay
        self.stuck = stuck  # load() never finishes: models a container still starting up
        self.ready = False
        self.batches: list[list[str]] = []
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def load(self) -> None:
        self.ready = not self.stuck

    @staticmethod
    def vector_for(text: str) -> list[float]:
        return [float(len(text)), float(ord(text[0]) if text else 0)] + [float(i) for i in range(6)]

    def encode(self, texts: list[str]) -> list[list[float]]:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.batches.append(list(texts))
        try:
            if self.delay:
                time.sleep(self.delay)
            return [self.vector_for(t) for t in texts]
        finally:
            with self._guard:
                self.active -= 1


@contextlib.contextmanager
def live_embedder(backend: FakeBackend, *, max_batch: int | None = None):
    """Run embedder.main.create_app on a real localhost port; yield its base URL."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(
        create_app(backend, max_batch=max_batch), log_level="warning", access_log=False,
    ))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "embedder test server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def closed_port_url() -> str:
    """A URL on localhost where nothing is listening -> an immediate connection refusal."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


def client_for(url: str, **kw) -> HttpEmbeddingBackend:
    kw.setdefault("expected_model", MODEL)
    return HttpEmbeddingBackend(url, **kw)


# --------------------------------------------------------------------------
# the embedder's own HTTP layer
# --------------------------------------------------------------------------
def test_health_and_info_when_the_model_is_loaded():
    with TestClient(create_app(FakeBackend())) as c:
        assert c.get("/health").json() == {"status": "ok", "model": MODEL}
        assert c.get("/info").json() == {"model": MODEL, "revision": "fake-rev", "dims": 8, "max_batch": 32}


def test_embed_returns_one_vector_per_text_in_order_with_model_and_dims():
    with TestClient(create_app(FakeBackend())) as c:
        r = c.post("/embed", json={"texts": ["alpha", "be", "c"]})
    body = r.json()
    assert r.status_code == 200
    assert (body["model"], body["dims"]) == (MODEL, 8)
    assert body["vectors"] == [FakeBackend.vector_for(t) for t in ["alpha", "be", "c"]]


def test_a_model_that_is_not_loaded_yet_answers_503_not_a_crash():
    with TestClient(create_app(FakeBackend(stuck=True))) as c:
        assert c.get("/health").status_code == 503
        assert c.get("/info").status_code == 503
        assert c.post("/embed", json={"texts": ["x"]}).status_code == 503


@pytest.mark.parametrize("payload", [{"texts": []}, {"texts": ["a", "b", "c"]}, {"texts": ["x" * 200_001]}])
def test_bad_batch_shapes_are_rejected_with_422(payload):
    with TestClient(create_app(FakeBackend(), max_batch=2)) as c:
        assert c.post("/embed", json=payload).status_code == 422


def test_validation_errors_never_echo_the_request_text():
    """FastAPI's default 422 body repeats the offending input. Here that input is a confidential
    document, so the embedder must answer with a generic message instead."""
    with TestClient(create_app(FakeBackend(), max_batch=2)) as c:
        wrong_type = c.post("/embed", json={"texts": SENTINEL})  # a string where a list is required
        too_many = c.post("/embed", json={"texts": [SENTINEL] * 3})
    for r in (wrong_type, too_many):
        assert r.status_code == 422
        assert SENTINEL not in r.text


def test_request_text_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    with TestClient(create_app(FakeBackend())) as c:
        assert c.post("/embed", json={"texts": [SENTINEL]}).status_code == 200
    assert SENTINEL not in caplog.text


def test_importing_the_embedder_service_does_not_import_the_model_libraries():
    """The API's tests (and image) have no torch; embedder.main must stay import-light. Run in a
    fresh interpreter so a torch installed in some dev environment cannot hide a regression."""
    code = ("import sys, embedder.main; "
            "bad = [m for m in ('torch', 'sentence_transformers', 'transformers') if m in sys.modules]; "
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


# --------------------------------------------------------------------------
# the API's client, over real HTTP
# --------------------------------------------------------------------------
def test_large_inputs_are_split_into_batches_and_order_is_preserved():
    fake = FakeBackend()
    texts = [f"chunk-{i}" * (i + 1) for i in range(5)]
    with live_embedder(fake) as url:
        vectors = client_for(url, batch_size=2).encode(texts)
    assert [len(b) for b in fake.batches] == [2, 2, 1]
    assert vectors == [FakeBackend.vector_for(t) for t in texts]


class WideFakeBackend(FakeBackend):
    """Same fake, but at the width the storage columns actually are (768, ADR-0019)."""

    dims = 768

    @staticmethod
    def vector_for(text: str) -> list[float]:
        return [float(len(text)), float(ord(text[0]) if text else 0)] + [float(i % 7) for i in range(766)]


def test_embedding_service_over_http_prefixes_and_labels_the_vectors_without_changing_them():
    fake = WideFakeBackend()
    with live_embedder(fake) as url:
        svc = EmbeddingService(client_for(url), MODEL)
        docs = svc.embed_documents(["first", "second"])
        query = svc.embed_query("what is this")
    assert fake.batches[0] == ["search_document: first", "search_document: second"]
    assert fake.batches[1] == ["search_query: what is this"]
    for emb, text in zip(docs + [query], ["search_document: first", "search_document: second",
                                           "search_query: what is this"]):
        assert emb["embedding_dims"] == 768 and emb["embedding_model"] == MODEL
        assert emb["embedding"] == WideFakeBackend.vector_for(text), "stored exactly as the model returned it"


def test_a_model_of_the_wrong_width_is_refused_not_padded_or_cut():
    """ADR-0019: the columns are 768 wide. An 8-wide (or a 1536-wide) model must raise, never be
    quietly padded or truncated into storage where it could not be compared with the rest."""
    with live_embedder(FakeBackend()) as url:  # FakeBackend reports 8 dims
        svc = EmbeddingService(client_for(url), MODEL)
        with pytest.raises(ValueError, match="needs a migration and a re-embed"):
            svc.embed_documents(["x"])
        with pytest.raises(ValueError, match="needs a migration and a re-embed"):
            svc.embed_query("x")


def test_native_dims_can_be_read_before_any_encode():
    with live_embedder(FakeBackend()) as url:
        assert client_for(url).native_dims == 8


def test_an_unreachable_embedder_raises_unavailable():
    with pytest.raises(EmbedderUnavailable):
        client_for(closed_port_url()).encode(["x"])


def test_a_slow_embedder_times_out_as_unavailable():
    with live_embedder(FakeBackend(delay=1.5)) as url:
        with pytest.raises(EmbedderUnavailable):
            client_for(url, timeout=0.2).encode(["x"])


def test_an_embedder_still_loading_its_model_raises_unavailable():
    with live_embedder(FakeBackend(stuck=True)) as url:
        with pytest.raises(EmbedderUnavailable):
            client_for(url).encode(["x"])


def test_a_model_name_mismatch_is_refused_and_is_not_treated_as_transient():
    """embedding_model is what retrieval filters on. Storing vectors under the wrong name would
    silently poison search, so a mismatch must stop ingestion, and say how to fix it."""
    with live_embedder(FakeBackend(name="some-other-model")) as url:
        for call in (lambda b: b.encode(["x"]), lambda b: b.native_dims):
            with pytest.raises(EmbedderError) as exc:
                call(client_for(url))
            assert not isinstance(exc.value, EmbedderUnavailable)
            assert "rebuild" in str(exc.value)


def test_a_request_the_embedder_rejects_is_an_error_not_a_retry():
    with live_embedder(FakeBackend(), max_batch=2) as url:
        with pytest.raises(EmbedderError) as exc:
            client_for(url, batch_size=5).encode(["a"] * 5)  # larger than the embedder's limit
    assert not isinstance(exc.value, EmbedderUnavailable)


def test_concurrent_requests_are_encoded_one_at_a_time():
    """One model instance, CPU-bound: the embedder serialises encodes (scale with replicas)."""
    fake = FakeBackend(delay=0.05)
    errors: list[Exception] = []

    def worker(url):
        try:
            client_for(url).encode(["x"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    with live_embedder(fake) as url:
        threads = [threading.Thread(target=worker, args=(url,)) for _ in range(6)]
        [t.start() for t in threads]
        [t.join(timeout=20) for t in threads]
    assert errors == []
    assert len(fake.batches) == 6 and fake.max_active == 1


# --------------------------------------------------------------------------
# what the ingest route does when the embedder is unavailable
# --------------------------------------------------------------------------
async def test_ingest_answers_503_and_writes_nothing_when_the_embedder_is_down():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    title = "embedder-down.py"
    try:
        api_app.dependency_overrides[get_embedding_service] = (
            lambda: EmbeddingService(client_for(closed_port_url()), MODEL)
        )
        async with await _make_client() as client:
            r = await client.post("/ingest/code", json={"title": title, "content": "x = 1\n" * 20},
                                  headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 503, r.text
        assert r.json()["detail"]["error"] == "embedder_unavailable"
        assert "try again" in r.json()["detail"]["detail"]

        async with AsyncSessionLocal() as db:
            n = (await db.execute(select(func.count()).select_from(Document).where(
                Document.tenant_id == tenant_id, Document.title == title))).scalar_one()
        assert n == 0, "an unavailable embedder must not leave a partial Document row behind"
    finally:
        api_app.dependency_overrides.pop(get_embedding_service, None)
        await _cleanup(tenant_id)


# --------------------------------------------------------------------------
# deployment contract: the architecture decisions that are easy to undo by accident
# --------------------------------------------------------------------------
def _package_names(requirements_path: Path) -> set[str]:
    names = set()
    for line in requirements_path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.add(line.split("==")[0].split("[")[0].strip().lower())
    return names


def test_the_api_image_carries_no_model_libraries_and_the_embedder_carries_them_pinned():
    api = _package_names(ROOT / "requirements.txt")
    assert not api & {"torch", "sentence-transformers", "transformers", "einops"}, (
        "ML libraries belong in embedder/requirements.txt (ADR-0012), not the API image")
    assert "httpx" in api, "the API needs httpx at run time to reach the embedder"
    embedder_lines = (ROOT / "embedder" / "requirements.txt").read_text()
    for pinned in ("torch==2.4.1+cpu", "sentence-transformers==3.1.1", "transformers==4.44.2", "einops==0.8.0"):
        assert pinned in embedder_lines


def test_compose_publishes_the_database_to_localhost_only():
    """Review gap closed 2026-10-25: a bare "5432:5432" listens on 0.0.0.0, i.e. the whole network."""
    yaml = pytest.importorskip("yaml")
    path = ROOT / "docker-compose.yml"
    if not path.exists():
        pytest.skip("docker-compose.yml is not present inside the API image")
    ports = yaml.safe_load(path.read_text())["services"]["db"]["ports"]
    assert ports, "if the database stops being published at all, update this test deliberately"
    for mapping in ports:
        assert str(mapping).startswith("127.0.0.1:"), f"database port {mapping!r} is not bound to localhost"


def test_compose_isolates_the_embedder_from_the_internet_and_the_host():
    yaml = pytest.importorskip("yaml")
    path = ROOT / "docker-compose.yml"
    if not path.exists():  # compose files are deliberately not copied into the API image (.dockerignore)
        pytest.skip("docker-compose.yml is not present inside the API image")
    compose = yaml.safe_load(path.read_text())
    embedder, api = compose["services"]["embedder"], compose["services"]["api"]
    assert embedder["networks"] == ["embed_net"], "embedder must be on the private network only"
    assert compose["networks"]["embed_net"]["internal"] is True, "that network must have no route out"
    assert "ports" not in embedder, "the embedder must never be published to the host"
    assert "embed_net" in api["networks"]
    assert api["depends_on"]["embedder"]["condition"] == "service_healthy"
    assert "hf_cache" not in str(compose), "no runtime model cache: the model is baked into the image"
    assert "EMBEDDING_MODEL_NAME" in embedder["build"]["args"]["MODEL_NAME"], "one variable drives both sides"


def test_embedder_dockerfile_proves_offline_loading_and_runs_unprivileged():
    path = ROOT / "embedder" / "Dockerfile"
    if not path.exists():  # excluded from the API image on purpose
        pytest.skip("embedder/Dockerfile is not present inside the API image")
    text = path.read_text()
    assert "HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python -m embedder.fetch_model --verify-offline" in text
    assert text.index("--verify-offline") < text.index("USER embedder"), "verify offline before dropping to the service user"
    assert "USER embedder" in text
    assert '"--workers", "1"' in text, "one model copy per container; scale with replicas"
