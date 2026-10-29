"""
The glue the embedder's Docker build and service start-up run, tested with a recording stand-in
for `sentence_transformers` (the real library and model need torch and ~550 MB of weights).

What this proves: the pinned commits from revisions.lock really reach the loader as the keyword
arguments the libraries expect, the build's offline check returns the right exit codes, and the
service builds its backend from the shipped lock. What it does NOT prove: that the real model loads
-- that is what the Docker build's own offline step and scripts/smoke_real_model.py are for.
"""
import sys
import types
from pathlib import Path

import pytest

from embedder import fetch_model
from embedder.backend import SentenceTransformerBackend
from embedder.main import _backend_from_env

MODEL = "nomic-ai/nomic-embed-text-v1.5"
CODE = "nomic-ai/nomic-bert-2048"
MODEL_SHA = "e9b6763023c676ca8431644204f50c2b100d9aab"
CODE_SHA = "7710840340a098cfb869c4f65e87cf2b1b70caca"


class _Vectors(list):
    def tolist(self):
        return list(self)


class RecordingSentenceTransformer:
    """Stands in for sentence_transformers.SentenceTransformer and remembers how it was built."""

    calls: list[tuple[tuple, dict]] = []
    dims = 8

    def __init__(self, *args, **kwargs):
        type(self).calls.append((args, kwargs))

    def get_sentence_embedding_dimension(self):
        return type(self).dims

    def encode(self, texts, **kwargs):
        return _Vectors([[0.0] * type(self).dims for _ in texts])


@pytest.fixture(autouse=True)
def fake_library(monkeypatch):
    RecordingSentenceTransformer.calls = []
    RecordingSentenceTransformer.dims = 8
    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = RecordingSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return RecordingSentenceTransformer


def test_pinned_commits_reach_the_loader_as_the_keyword_arguments_the_libraries_accept(fake_library):
    SentenceTransformerBackend(MODEL, model_revision=MODEL_SHA, code_revision=CODE_SHA).load()
    (args, kwargs), = fake_library.calls
    assert args == (MODEL,)
    assert kwargs == {
        "trust_remote_code": True,
        "revision": MODEL_SHA,
        "config_kwargs": {"code_revision": CODE_SHA},
        "model_kwargs": {"code_revision": CODE_SHA},
    }


def test_nothing_is_pinned_when_the_lock_says_unlocked(fake_library):
    SentenceTransformerBackend(MODEL).load()
    (_, kwargs), = fake_library.calls
    assert kwargs == {"trust_remote_code": True}


def test_a_backend_is_not_ready_until_loaded_and_then_reports_its_width(fake_library):
    backend = SentenceTransformerBackend(MODEL)
    assert backend.ready is False
    backend.load()
    assert backend.ready is True and backend.dims == 8
    assert backend.encode(["a", "b"]) == [[0.0] * 8, [0.0] * 8]


def test_the_service_builds_its_backend_from_the_shipped_lock(monkeypatch, fake_library):
    monkeypatch.setenv("EMBEDDER_MODEL_NAME", MODEL)
    backend = _backend_from_env()
    backend.load()
    (_, kwargs), = fake_library.calls
    assert kwargs["revision"] == MODEL_SHA
    assert kwargs["config_kwargs"] == {"code_revision": CODE_SHA}
    assert backend.revision == MODEL_SHA


def _hub(tmp_path: Path, repos: dict[str, str]) -> Path:
    hub = tmp_path / "hub"
    for repo, commit in repos.items():
        (hub / f"models--{repo.replace('/', '--')}" / "snapshots" / commit).mkdir(parents=True)
    return tmp_path  # this is HF_HOME


@pytest.fixture
def build_env(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBEDDER_MODEL_NAME", MODEL)
    monkeypatch.setenv("EMBEDDER_EXPECTED_DIMS", "8")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("REVISION_LOCK_MODE", "strict")
    monkeypatch.setenv("HF_HOME", str(_hub(tmp_path, {MODEL: MODEL_SHA, CODE: CODE_SHA})))


def test_the_offline_check_passes_when_the_cache_matches_the_lock(build_env):
    assert fetch_model.main(["--verify-offline"]) == 0


def test_the_offline_check_fails_the_build_in_strict_mode_when_the_cache_drifted(build_env, monkeypatch, tmp_path):
    monkeypatch.setenv("HF_HOME", str(_hub(tmp_path / "drift", {MODEL: MODEL_SHA, CODE: "f" * 40})))
    assert fetch_model.main(["--verify-offline"]) == 1


def test_in_warn_mode_drift_is_reported_but_does_not_stop_the_first_build(build_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("REVISION_LOCK_MODE", "warn")
    monkeypatch.setenv("HF_HOME", str(_hub(tmp_path / "drift", {MODEL: MODEL_SHA, CODE: "f" * 40})))
    assert fetch_model.main(["--verify-offline"]) == 0
    assert "WARNING" in capsys.readouterr().err


def test_a_model_of_the_wrong_width_fails_the_build(build_env, fake_library):
    fake_library.dims = 16  # the image would otherwise silently serve vectors of the wrong size
    assert fetch_model.main(["--verify-offline"]) == 1
