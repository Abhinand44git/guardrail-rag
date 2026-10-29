"""
Phase 5: POST /retrieve, tested end to end over ASGI against the real Postgres + pgvector.

This endpoint is where the security model pays off or fails: it decides which stored text a person
gets to hand to an AI model. So these tests are written as ATTACKS on it -- each one sets up data
where the forbidden chunk is the single best match for the question, and checks it still does not
come back. A test where the forbidden chunk is a poor match proves nothing.

The embedding model is replaced by a fake (no model, no network) whose vectors are meaningful
enough to rank: a hashed bag of words, so a text is most similar to itself and to texts sharing its
words. Vectors are deliberately NOT unit length, like the real model's (ADR-0010). Where exact
vectors matter (cosine vs L2 ordering) the tests script them directly.
"""
import json
import re
import uuid
import zlib
from types import SimpleNamespace

import numpy as np
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.embeddings import EmbedderUnavailable, EmbeddingService, get_embedding_service
from app.main import app
from app.models import Document, DocumentVector, ProjectMemory, Role, Tenant, User
from app.security import create_operator_token, create_user_token, hash_password

MODEL = "fake-test-embedding-model"
DIMS = 768
PREFIX = re.compile(r"^(search_document|search_query): ")


def bow(text_: str) -> list[float]:
    """Hashed bag of words: same words -> same direction. Raw counts, so NOT unit length."""
    v = [0.0] * DIMS
    for tok in re.findall(r"\w+", text_.lower()):
        v[zlib.crc32(tok.encode()) % DIMS] += 1.0
    return v


def cosine(a, b) -> float:
    a, b = np.asarray(a, dtype=np.float64), np.asarray(b, dtype=np.float64)
    return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))


class FakeBackend:
    native_dims = DIMS

    def __init__(self):
        self.calls: list[list[str]] = []
        self.scripted: dict[str, list[float]] = {}  # unprefixed text -> exact vector
        self.fail: Exception | None = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail is not None:
            raise self.fail
        out = []
        for t in texts:
            bare = PREFIX.sub("", t)
            out.append(self.scripted.get(bare, bow(bare)))
        return out


@pytest.fixture
def backend():
    b = FakeBackend()
    svc = EmbeddingService(b, MODEL)
    app.dependency_overrides[get_embedding_service] = lambda: svc
    yield b
    app.dependency_overrides.pop(get_embedding_service, None)


# --- world: two tenants, one user per role in each ---------------------------------------------
async def _purge(*tenant_ids):
    """Delete everything these tenants own. Memory rows go first, for ALL the tenants, because a
    row in one tenant may name a user of another as its author (the cross-tenant attack tests
    create exactly that), and the user cannot be deleted while such a row exists."""
    async with AsyncSessionLocal() as db:
        for t in tenant_ids:
            await db.execute(text("DELETE FROM project_memory WHERE tenant_id = :t"), {"t": t})
        for t in tenant_ids:
            for table in ("document_vectors", "documents", "users"):
                await db.execute(text(f"DELETE FROM {table} WHERE tenant_id = :t"), {"t": t})
            await db.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": t})
        await db.commit()


async def _make_tenant() -> uuid.UUID:
    tid = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tid, name=f"retrieve-test-{tid.hex[:8]}"))
        await db.commit()
    return tid


async def _make_user(tenant_id, role) -> uuid.UUID:
    uid = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(id=uid, tenant_id=tenant_id, username=f"u-{uid.hex[:8]}",
                    email=f"{uid.hex[:10]}@retrieve-test.local",
                    hashed_password=hash_password("x"), role=role))
        await db.commit()
    return uid


@pytest.fixture
async def world():
    """tenants A and B; in each an Admin (1), Developer (2), Trainee (3) with a ready bearer token."""
    w = SimpleNamespace(tenants=[])
    for name in ("a", "b"):
        tid = await _make_tenant()
        w.tenants.append(tid)
        users = {r: await _make_user(tid, r) for r in (1, 2, 3)}
        setattr(w, name, SimpleNamespace(
            tenant=tid, users=users,
            token={r: create_user_token(users[r], tid, r) for r in (1, 2, 3)},
        ))
    yield w
    await _purge(*w.tenants)


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# --- seeding rows directly (bypasses /ingest so clearance and vectors are fully controlled) ----
async def _chunk(tenant, title, text_, min_role, *, model=MODEL, vec=None) -> uuid.UUID:
    async with AsyncSessionLocal() as db:
        doc = Document(id=uuid.uuid4(), tenant_id=tenant, title=title, source_type="markdown",
                       min_role_required=min_role)
        db.add(doc)
        await db.flush()
        chunk_id = uuid.uuid4()
        db.add(DocumentVector(id=chunk_id, document_id=doc.id, tenant_id=tenant, chunk_text=text_,
                              embedding=vec if vec is not None else bow(text_), embedding_model=model,
                              embedding_dims=DIMS, chunk_strategy="test"))
        await db.commit()
    return chunk_id


async def _memory(tenant, author, project, phase, text_, min_role, *, model=MODEL, vec=None) -> uuid.UUID:
    mid = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(ProjectMemory(id=mid, tenant_id=tenant, project_id=project, sdlc_phase=phase, content=text_,
                             min_role_required=min_role, created_by_user_id=author,
                             embedding=vec if vec is not None else bow(text_), embedding_model=model,
                             embedding_dims=DIMS))
        await db.commit()
    return mid


async def _retrieve(token, **body):
    async with await _client() as c:
        return await c.post("/retrieve", json=body, headers=_auth(token) if token else {})


def _titles(resp):
    return {d["title"] for d in resp.json()["documents"]}


def _memory_texts(resp):
    return {m["text"] for m in resp.json()["memory"]}


# ================================================================================================
# 1. Clearance: the forbidden chunk is the BEST possible match and must still not appear
# ================================================================================================
SECRET = "confidential salary bands for the engineering department"
DEVNOTE = "deployment runbook for the engineering department servers"
PUBLIC = "onboarding handbook for the engineering department newcomers"


@pytest.mark.parametrize("role,expected", [
    (1, {"secret", "devnote", "public"}),
    (2, {"devnote", "public"}),
    (3, {"public"}),
])
async def test_each_role_only_ever_gets_chunks_at_or_below_its_clearance(world, backend, role, expected):
    await _chunk(world.a.tenant, "secret", SECRET, 1)
    await _chunk(world.a.tenant, "devnote", DEVNOTE, 2)
    await _chunk(world.a.tenant, "public", PUBLIC, 3)
    # The question is word-for-word the Admin-only chunk: the strongest possible pull toward it.
    r = await _retrieve(world.a.token[role], query=SECRET, k=20)
    assert r.status_code == 200, r.text
    assert _titles(r) == expected


async def test_the_admin_only_chunk_is_ranked_first_for_an_admin_so_the_attack_above_is_a_real_one(world, backend):
    """Control for the test above: if the secret were not the best match for an Admin, hiding it
    from a Trainee would prove nothing."""
    await _chunk(world.a.tenant, "secret", SECRET, 1)
    await _chunk(world.a.tenant, "public", PUBLIC, 3)
    docs = (await _retrieve(world.a.token[1], query=SECRET, k=5)).json()["documents"]
    assert docs[0]["title"] == "secret" and docs[0]["score"] > 0.99


async def test_clearance_direction_is_not_inverted_a_trainee_level_chunk_is_open_to_everyone(world, backend):
    await _chunk(world.a.tenant, "public", PUBLIC, 3)
    for role in (1, 2, 3):
        assert _titles(await _retrieve(world.a.token[role], query=PUBLIC)) == {"public"}


# ================================================================================================
# 2. Tenant isolation
# ================================================================================================
async def test_another_tenants_best_match_never_comes_back_even_for_an_admin(world, backend):
    mine = await _chunk(world.a.tenant, "mine", PUBLIC, 3, vec=bow("completely unrelated words here"))
    theirs = await _chunk(world.b.tenant, "theirs", PUBLIC, 3)  # B's chunk matches the question perfectly
    r = await _retrieve(world.a.token[1], query=PUBLIC, k=20)
    ids = {d["chunk_id"] for d in r.json()["documents"]}
    assert str(mine) in ids and str(theirs) not in ids
    assert "theirs" not in json.dumps(r.json())


async def test_two_tenants_with_identical_text_each_see_only_their_own_ids(world, backend):
    a_id = await _chunk(world.a.tenant, "same", PUBLIC, 3)
    b_id = await _chunk(world.b.tenant, "same", PUBLIC, 3)
    ra = await _retrieve(world.a.token[3], query=PUBLIC)
    rb = await _retrieve(world.b.token[3], query=PUBLIC)
    assert [d["chunk_id"] for d in ra.json()["documents"]] == [str(a_id)]
    assert [d["chunk_id"] for d in rb.json()["documents"]] == [str(b_id)]


async def test_a_tenant_or_role_supplied_in_the_request_body_changes_nothing(world, backend):
    """Nothing in the body may steer the tenant or the clearance: they come from the database row
    of the authenticated user. Extra fields are simply ignored."""
    await _chunk(world.b.tenant, "theirs", PUBLIC, 3)
    await _chunk(world.a.tenant, "secret", SECRET, 1)
    r = await _retrieve(world.a.token[3], query=PUBLIC, k=20,
                        tenant_id=str(world.b.tenant), role=1, min_role_required=1, user_id=str(world.a.users[1]))
    assert r.status_code == 200
    assert r.json()["documents"] == [] or _titles(r) == set()


# ================================================================================================
# 3. The caller's CURRENT role, not the one frozen in their token
# ================================================================================================
async def test_a_token_that_claims_admin_but_whose_user_is_now_a_trainee_gets_trainee_results(world, backend):
    await _chunk(world.a.tenant, "secret", SECRET, 1)
    await _chunk(world.a.tenant, "public", PUBLIC, 3)
    stale_admin_token = create_user_token(world.a.users[3], world.a.tenant, 1)  # claims role 1; DB says 3
    r = await _retrieve(stale_admin_token, query=SECRET, k=20)
    assert r.status_code == 200
    assert _titles(r) == {"public"}


async def test_demoting_a_user_takes_effect_on_their_very_next_retrieve_not_when_their_token_expires(world, backend):
    await _chunk(world.a.tenant, "secret", SECRET, 1)
    token = world.a.token[1]
    assert _titles(await _retrieve(token, query=SECRET)) == {"secret"}
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET role = 3 WHERE id = :u"), {"u": world.a.users[1]})
        await db.commit()
    assert _titles(await _retrieve(token, query=SECRET)) == set()


async def test_a_token_whose_tenant_does_not_match_the_users_own_tenant_is_refused(world, backend):
    mismatched = create_user_token(world.a.users[1], world.b.tenant, 1)
    assert (await _retrieve(mismatched, query="anything")).status_code == 401


# ================================================================================================
# 4. Authentication
# ================================================================================================
async def test_no_token_garbage_token_and_operator_token_are_all_rejected(world, backend):
    assert (await _retrieve(None, query="x")).status_code == 401
    assert (await _retrieve("not-a-jwt", query="x")).status_code == 401
    assert (await _retrieve(create_operator_token(uuid.uuid4()), query="x")).status_code == 401


async def test_a_deactivated_user_is_rejected_on_the_next_request(world, backend):
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": world.a.users[2]})
        await db.commit()
    assert (await _retrieve(world.a.token[2], query="x")).status_code == 401


# ================================================================================================
# 5. Ranking: cosine, not L2; the embedding_model filter; zero vectors
# ================================================================================================
def _unit(i, n=DIMS):
    v = [0.0] * n
    v[i] = 1.0
    return v


async def test_results_are_ranked_by_cosine_not_by_euclidean_distance(world, backend):
    """ADR-0010. Stored vectors are not unit length, so L2 and cosine disagree. Query q = [1,0,..].
    LONG = [100, 1, 0..] points almost exactly along q (cosine 0.99995) but is far away in L2 (~99).
    SHORT = [0.5, 0.5, 0..] is 45 degrees off (cosine 0.707) but close in L2 (~0.71). Cosine must
    put LONG first; an L2 ordering would put SHORT first."""
    long_v = [100.0, 1.0] + [0.0] * (DIMS - 2)
    short_v = [0.5, 0.5] + [0.0] * (DIMS - 2)
    backend.scripted["the question"] = _unit(0)
    await _chunk(world.a.tenant, "SHORT", "short", 3, vec=short_v)
    await _chunk(world.a.tenant, "LONG", "long", 3, vec=long_v)
    docs = (await _retrieve(world.a.token[3], query="the question")).json()["documents"]
    assert [d["title"] for d in docs] == ["LONG", "SHORT"]
    assert docs[0]["score"] == pytest.approx(cosine(_unit(0), long_v), abs=1e-4)
    assert docs[1]["score"] == pytest.approx(cosine(_unit(0), short_v), abs=1e-4)


async def test_scores_are_descending_and_k_limits_each_list(world, backend):
    for i in range(8):
        await _chunk(world.a.tenant, f"c{i}", f"{PUBLIC} extra{i} " + "word " * i, 3)
    docs = (await _retrieve(world.a.token[3], query=PUBLIC, k=3)).json()["documents"]
    assert len(docs) == 3
    scores = [d["score"] for d in docs]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.parametrize("k", [0, -1, 21, 1000])
async def test_k_outside_1_to_20_is_rejected(world, backend, k):
    assert (await _retrieve(world.a.token[3], query="x", k=k)).status_code == 422


async def test_vectors_from_a_different_embedding_model_are_never_returned(world, backend):
    """ADR-0005: vectors from different models are not comparable. A row stored under another
    model name must be invisible even when its vector is a perfect match."""
    await _chunk(world.a.tenant, "old-model", PUBLIC, 3, model="some-older-model")
    await _chunk(world.a.tenant, "current", "something else entirely", 3)
    r = await _retrieve(world.a.token[3], query=PUBLIC, k=20)
    assert _titles(r) == {"current"}
    assert r.json()["embedding_model"] == MODEL


async def test_memory_stored_under_a_different_embedding_model_is_never_returned(world, backend):
    """Found by mutation testing: the embedding_model filter on the MEMORY query could be deleted
    without any test noticing, because only documents were covered. Same rule, same attack."""
    p = uuid.uuid4()
    await _memory(world.a.tenant, world.a.users[1], p, "design", "old model note", 3,
                  model="some-older-model", vec=bow("refund policy"))
    await _memory(world.a.tenant, world.a.users[1], p, "design", "current model note", 3, vec=bow("unrelated words"))
    r = await _retrieve(world.a.token[3], query="refund policy", k=20)
    assert _memory_texts(r) == {"current model note"}


async def test_an_all_zero_stored_memory_vector_is_skipped_instead_of_breaking_the_response(world, backend):
    await _memory(world.a.tenant, world.a.users[1], uuid.uuid4(), "design", "zero note", 3, vec=[0.0] * DIMS)
    await _memory(world.a.tenant, world.a.users[1], uuid.uuid4(), "design", "real note", 3, vec=bow("refund policy"))
    r = await _retrieve(world.a.token[3], query="refund policy", k=20)
    assert r.status_code == 200 and _memory_texts(r) == {"real note"} and "NaN" not in r.text


async def test_an_all_zero_stored_vector_is_skipped_instead_of_breaking_the_response(world, backend):
    """Cosine distance to a zero vector is NaN, which is not valid JSON. It must never reach the client."""
    await _chunk(world.a.tenant, "zero", "zero", 3, vec=[0.0] * DIMS)
    await _chunk(world.a.tenant, "real", PUBLIC, 3)
    r = await _retrieve(world.a.token[3], query=PUBLIC, k=20)
    assert r.status_code == 200
    assert _titles(r) == {"real"}
    assert "NaN" not in r.text


async def test_empty_knowledge_base_returns_two_empty_lists(world, backend):
    r = await _retrieve(world.a.token[3], query="anything at all")
    assert r.status_code == 200 and r.json()["documents"] == [] and r.json()["memory"] == []


# ================================================================================================
# 6. Project memory in retrieval: clearance, tenant, project label, SDLC phase
# ================================================================================================
@pytest.mark.parametrize("role,expected", [
    (1, {"m-secret", "m-dev", "m-open"}),
    (2, {"m-dev", "m-open"}),
    (3, {"m-open"}),
])
async def test_memory_obeys_the_same_clearance_rule_as_documents(world, backend, role, expected):
    p = uuid.uuid4()
    author = world.a.users[1]
    await _memory(world.a.tenant, author, p, "design", "m-secret decision about salary", 1, vec=bow("decision salary"))
    await _memory(world.a.tenant, author, p, "design", "m-dev decision about deployment", 2, vec=bow("decision salary"))
    await _memory(world.a.tenant, author, p, "design", "m-open decision about naming", 3, vec=bow("decision salary"))
    r = await _retrieve(world.a.token[role], query="decision salary", k=20)
    assert {t.split()[0] for t in _memory_texts(r)} == expected


async def test_an_author_gets_their_own_admin_only_memory_back_from_retrieve_and_nobody_else_does(world, backend):
    """ADR-0023: same single rule as the list endpoint. The author finds their own note even at the
    Admin-only level; another Developer and a Trainee do not; an Admin does."""
    p = uuid.uuid4()
    await _memory(world.a.tenant, world.a.users[2], p, "design", "my private design decision", 1, vec=bow("decision"))
    assert _memory_texts(await _retrieve(world.a.token[2], query="decision")) == {"my private design decision"}
    assert _memory_texts(await _retrieve(world.a.token[1], query="decision")) == {"my private design decision"}
    other_dev = create_user_token(await _make_user(world.a.tenant, 2), world.a.tenant, 2)
    assert _memory_texts(await _retrieve(other_dev, query="decision")) == set()
    assert _memory_texts(await _retrieve(world.a.token[3], query="decision")) == set()


async def test_authorship_never_reaches_across_tenants_in_retrieve_either(world, backend):
    """An entry stored in tenant B but naming a tenant-A user as author must not reach that user."""
    await _memory(world.b.tenant, world.a.users[2], uuid.uuid4(), "design", "lives in tenant B", 1, vec=bow("decision"))
    r = await _retrieve(world.a.token[2], query="decision", k=20)
    assert r.status_code == 200 and r.json()["memory"] == []


async def test_an_authors_own_entry_still_obeys_the_model_filter_and_the_project_label(world, backend):
    me, p1 = world.a.users[2], uuid.uuid4()
    await _memory(world.a.tenant, me, p1, "design", "own old-model note", 1, model="some-older-model", vec=bow("decision"))
    await _memory(world.a.tenant, me, p1, "design", "own current note", 1, vec=bow("decision"))
    await _memory(world.a.tenant, me, uuid.uuid4(), "design", "own other-project note", 1, vec=bow("decision"))
    r = await _retrieve(world.a.token[2], query="decision", project_id=str(p1), k=20)
    assert _memory_texts(r) == {"own current note"}


async def test_another_tenants_memory_never_comes_back(world, backend):
    p = uuid.uuid4()
    await _memory(world.b.tenant, world.b.users[1], p, "design", "their private decision", 3, vec=bow("decision"))
    await _memory(world.a.tenant, world.a.users[1], p, "design", "our decision", 3, vec=bow("unrelated"))
    # Same project id on purpose: project_id is a label, not a wall.
    r = await _retrieve(world.a.token[1], query="decision", project_id=str(p), k=20)
    assert _memory_texts(r) == {"our decision"}


async def test_project_id_only_narrows_memory_and_never_widens_access(world, backend):
    p1, p2 = uuid.uuid4(), uuid.uuid4()
    a = world.a.users[1]
    await _memory(world.a.tenant, a, p1, "design", "p1 open note", 3, vec=bow("note"))
    await _memory(world.a.tenant, a, p2, "design", "p2 open note", 3, vec=bow("note"))
    await _memory(world.a.tenant, a, p1, "design", "p1 secret note", 1, vec=bow("note"))
    await _chunk(world.a.tenant, "doc", "note about documents", 3)

    both = await _retrieve(world.a.token[3], query="note", k=20)
    assert _memory_texts(both) == {"p1 open note", "p2 open note"}          # no filter: all projects
    only1 = await _retrieve(world.a.token[3], query="note", project_id=str(p1), k=20)
    assert _memory_texts(only1) == {"p1 open note"}                          # narrowed; secret still hidden
    unknown = await _retrieve(world.a.token[3], query="note", project_id=str(uuid.uuid4()), k=20)
    assert unknown.status_code == 200 and unknown.json()["memory"] == []     # unknown label: empty, not an error
    assert _titles(unknown) == {"doc"}, "documents have no project, so the label does not affect them"


async def test_phase_filter_includes_earlier_phases_by_default_and_exact_when_asked(world, backend):
    p = uuid.uuid4()
    for phase in ("planning", "analysis", "design", "development", "testing", "deployment"):
        await _memory(world.a.tenant, world.a.users[1], p, phase, f"note from {phase}", 3, vec=bow("note"))
    tok = world.a.token[3]
    texts = lambda r: {t.replace("note from ", "") for t in _memory_texts(r)}  # noqa: E731

    assert texts(await _retrieve(tok, query="note", k=20)) == {
        "planning", "analysis", "design", "development", "testing", "deployment"}
    assert texts(await _retrieve(tok, query="note", phase="design", k=20)) == {"planning", "analysis", "design"}
    assert texts(await _retrieve(tok, query="note", phase="design", include_earlier_phases=False, k=20)) == {"design"}
    assert texts(await _retrieve(tok, query="note", phase="planning", k=20)) == {"planning"}
    assert texts(await _retrieve(tok, query="note", phase="deployment", k=20)) == {
        "planning", "analysis", "design", "development", "testing", "deployment"}


async def test_the_phase_filter_cannot_reveal_a_memory_entry_the_clearance_hides(world, backend):
    await _memory(world.a.tenant, world.a.users[1], uuid.uuid4(), "design", "hidden design note", 1, vec=bow("note"))
    r = await _retrieve(world.a.token[3], query="note", phase="design", k=20)
    assert r.json()["memory"] == []


@pytest.mark.parametrize("bad", [{"phase": "maintenance"}, {"project_id": "not-a-uuid"}, {"include_earlier_phases": "maybe"}])
async def test_malformed_filters_are_rejected(world, backend, bad):
    assert (await _retrieve(world.a.token[3], query="x", **bad)).status_code == 422


@pytest.mark.parametrize("query", ["", "   ", "x" * 2001])
async def test_blank_or_oversized_queries_are_rejected(world, backend, query):
    assert (await _retrieve(world.a.token[3], query=query)).status_code == 422


# ================================================================================================
# 7. The shape of the answer, and what must NOT be in it
# ================================================================================================
async def test_response_shape_has_two_labelled_lists_and_no_internal_fields(world, backend):
    p = uuid.uuid4()
    await _chunk(world.a.tenant, "doc", PUBLIC, 3)
    await _memory(world.a.tenant, world.a.users[1], p, "design", "a decision about handbooks", 3, vec=bow(PUBLIC))
    body = (await _retrieve(world.a.token[3], query=PUBLIC)).json()
    assert set(body) == {"embedding_model", "documents", "memory"}
    assert set(body["documents"][0]) == {"chunk_id", "document_id", "title", "source_type", "text", "score"}
    assert set(body["memory"][0]) == {"memory_id", "project_id", "phase", "text", "created_by_user_id",
                                      "created_at", "score"}
    raw = json.dumps(body)
    for forbidden in ("embedding\"", "min_role", "tenant_id", "clearance"):
        assert forbidden not in raw, forbidden


async def test_the_question_is_embedded_with_the_query_prefix(world, backend):
    await _retrieve(world.a.token[3], query="how do refunds work")
    assert backend.calls == [["search_query: how do refunds work"]]


async def test_nothing_is_written_by_a_retrieve_call(world, backend):
    await _chunk(world.a.tenant, "doc", PUBLIC, 3)
    async def counts():
        async with AsyncSessionLocal() as db:
            return [(await db.execute(text(f"SELECT count(*) FROM {t}"))).scalar_one()
                    for t in ("documents", "document_vectors", "project_memory", "users", "tenants")]
    before = await counts()
    await _retrieve(world.a.token[3], query=PUBLIC)
    assert await counts() == before


# ================================================================================================
# 8. Failure behaviour: safe errors, nothing leaked
# ================================================================================================
async def test_an_unreachable_embedder_gives_a_safe_503(world, backend):
    backend.fail = EmbedderUnavailable("embedder unreachable (ConnectError) host=embedder:8000")
    r = await _retrieve(world.a.token[3], query="x")
    assert r.status_code == 503
    assert r.json() == {"detail": {"error": "embedder_unavailable",
                                   "detail": "The embedding service is temporarily unavailable — please try again shortly."}}
    assert "embedder:8000" not in r.text and "ConnectError" not in r.text


async def test_an_unexpected_embedding_failure_gives_a_safe_500_without_the_exception_text(world, backend):
    backend.fail = RuntimeError("CUDA out of memory at /srv/secret/path tensor.cpp:77")
    r = await _retrieve(world.a.token[3], query="x")
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "server_error"
    assert "CUDA" not in r.text and "/srv/secret" not in r.text


async def test_a_wrong_width_model_is_a_safe_500_not_a_padded_vector(world, backend):
    backend.scripted["x"] = [1.0] * 8  # 8-wide: must be refused, never padded into a comparison
    r = await _retrieve(world.a.token[3], query="x")
    assert r.status_code == 500 and "dims" not in r.text
