"""
Phase 5: POST /project-memory and GET /project-memory, end to end over ASGI against real Postgres.

Same standard as test_retrieve_endpoint.py (and it reuses that module's fake embedder and world
fixture): the security-relevant cases are attacks, not happy paths. Retrieval OVER memory (ranking,
phase/project narrowing) is tested in test_retrieve_endpoint.py; this file covers writing and
listing.
"""
import uuid

import pytest
from sqlalchemy import select, text

from app.security import create_user_token

from app.database import AsyncSessionLocal
from app.embeddings import EmbedderUnavailable
from app.models import ProjectMemory
from tests.test_retrieve_endpoint import (  # noqa: F401  (fixtures are used by name)
    DIMS, MODEL, FakeBackend, _auth, _client, _make_user, _memory, backend, bow, world,
)


async def _post(token, **body):
    async with await _client() as c:
        return await c.post("/project-memory", json=body, headers=_auth(token) if token else {})


async def _get(token, **params):
    async with await _client() as c:
        return await c.get("/project-memory", params=params, headers=_auth(token) if token else {})


async def _rows(tenant) -> list[ProjectMemory]:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(ProjectMemory).where(ProjectMemory.tenant_id == tenant))).scalars().all()


def _body(**over):
    return {"project_id": str(uuid.uuid4()), "phase": "design", "content": "We chose soft deactivation.", **over}


# ================================================================================================
# POST: who may write, and what gets stored
# ================================================================================================
@pytest.mark.parametrize("role,status", [(1, 201), (2, 201), (3, 401)])
async def test_admin_and_developer_can_write_trainee_cannot(world, backend, role, status):
    r = await _post(world.a.token[role], **_body())
    assert r.status_code == status, r.text
    assert len(await _rows(world.a.tenant)) == (1 if status == 201 else 0)


async def test_no_token_is_rejected_and_nothing_is_embedded_or_stored(world, backend):
    r = await _post(None, **_body())
    assert r.status_code == 401
    assert backend.calls == [] and await _rows(world.a.tenant) == []


async def test_a_new_entry_defaults_to_admin_only(world, backend):
    r = await _post(world.a.token[2], **_body())
    assert r.json()["min_role_required"] == 1
    assert (await _rows(world.a.tenant))[0].min_role_required == 1


async def test_the_writer_may_choose_a_looser_level_at_creation_like_ingestion(world, backend):
    r = await _post(world.a.token[2], **_body(min_role_required=3))
    assert r.status_code == 201 and r.json()["min_role_required"] == 3


async def test_the_stored_row_is_exactly_what_was_asked_and_who_asked(world, backend):
    pid = str(uuid.uuid4())
    r = await _post(world.a.token[2], **_body(project_id=pid, phase="testing", content="Use pytest-asyncio."))
    assert r.status_code == 201
    row = (await _rows(world.a.tenant))[0]
    assert str(row.id) == r.json()["id"]
    assert (str(row.project_id), row.sdlc_phase, row.content) == (pid, "testing", "Use pytest-asyncio.")
    assert row.created_by_user_id == world.a.users[2]
    assert row.tenant_id == world.a.tenant
    assert row.embedding_model == MODEL and row.embedding_dims == DIMS and len(row.embedding) == DIMS
    assert list(row.embedding) == pytest.approx(bow("Use pytest-asyncio."))


async def test_a_note_is_embedded_as_a_document_not_as_a_query(world, backend):
    await _post(world.a.token[1], **_body(content="Decision text."))
    assert backend.calls == [["search_document: Decision text."]]


async def test_the_tenant_comes_from_the_caller_never_from_the_body(world, backend):
    r = await _post(world.a.token[1], **_body(tenant_id=str(world.b.tenant), created_by_user_id=str(world.b.users[1])))
    assert r.status_code == 201
    assert len(await _rows(world.a.tenant)) == 1 and await _rows(world.b.tenant) == []
    assert (await _rows(world.a.tenant))[0].created_by_user_id == world.a.users[1]


async def test_the_writers_current_role_is_checked_not_the_one_in_their_token(world, backend):
    """A Developer demoted to Trainee after logging in must not be able to keep writing."""
    token = world.a.token[2]
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET role = 3 WHERE id = :u"), {"u": world.a.users[2]})
        await db.commit()
    assert (await _post(token, **_body())).status_code == 401
    assert await _rows(world.a.tenant) == []


@pytest.mark.parametrize("bad", [
    {"phase": "maintenance"}, {"phase": ""}, {"project_id": "nope"}, {"content": ""}, {"content": "   "},
    {"content": "x" * 4001}, {"min_role_required": 0}, {"min_role_required": 4}, {"min_role_required": "admin"},
])
async def test_invalid_input_is_rejected_before_anything_is_embedded_or_stored(world, backend, bad):
    r = await _post(world.a.token[1], **_body(**bad))
    assert r.status_code == 422
    assert backend.calls == [] and await _rows(world.a.tenant) == []


async def test_content_at_exactly_the_limit_is_accepted(world, backend):
    assert (await _post(world.a.token[1], **_body(content="x" * 4000))).status_code == 201


async def test_an_unreachable_embedder_gives_a_safe_503_and_stores_nothing(world, backend):
    backend.fail = EmbedderUnavailable("embedder unreachable host=embedder:8000")
    r = await _post(world.a.token[1], **_body())
    assert r.status_code == 503 and "embedder:8000" not in r.text
    assert await _rows(world.a.tenant) == []


async def test_an_unexpected_embedding_failure_gives_a_safe_500_and_stores_nothing(world, backend):
    backend.fail = RuntimeError("internal /srv/model/path blew up")
    r = await _post(world.a.token[1], **_body())
    assert r.status_code == 500 and "/srv/model" not in r.text
    assert await _rows(world.a.tenant) == []


async def test_the_response_never_contains_the_vector(world, backend):
    body = (await _post(world.a.token[1], **_body())).json()
    assert set(body) == {"id", "project_id", "phase", "content", "min_role_required", "created_by_user_id", "created_at"}


# ================================================================================================
# Authors always see their own entries (ADR-0023) -- and nothing else widens
# ================================================================================================
async def _second_developer(world):
    uid = await _make_user(world.a.tenant, 2)
    return uid, create_user_token(uid, world.a.tenant, 2)


async def test_an_author_reads_back_their_own_entry_even_at_the_admin_only_default(world, backend):
    await _post(world.a.token[2], **_body(content="Admin-only by default."))
    mine = (await _get(world.a.token[2])).json()
    assert [i["content"] for i in mine["items"]] == ["Admin-only by default."] and mine["total"] == 1
    assert mine["items"][0]["min_role_required"] == 1, "it is still Admin-only; the author just wrote it"


async def test_a_different_developer_in_the_same_tenant_still_cannot_see_it(world, backend):
    await _post(world.a.token[2], **_body(content="Mine, Admin-only."))
    _, other_token = await _second_developer(world)
    assert (await _get(other_token)).json()["items"] == []
    assert (await _get(world.a.token[3])).json()["items"] == []        # nor a Trainee
    assert len((await _get(world.a.token[1])).json()["items"]) == 1    # Admins still see everything


async def test_authorship_never_reaches_across_tenants(world, backend):
    """The dangerous mistake is `tenant AND clearance OR author`, which SQL reads as
    `(tenant AND clearance) OR author`. Force the situation directly: an entry that lives in
    tenant B but names a tenant-A user as its author. That user must not see it."""
    a_dev = world.a.users[2]
    await _memory(world.b.tenant, a_dev, uuid.uuid4(), "design", "lives in tenant B", 1, vec=bow("x"))
    await _memory(world.a.tenant, a_dev, uuid.uuid4(), "design", "lives in tenant A", 1, vec=bow("x"))
    assert [i["content"] for i in (await _get(world.a.token[2])).json()["items"]] == ["lives in tenant A"]


async def test_an_author_whose_role_dropped_still_sees_their_own_but_not_other_peoples_admin_only_entries(world, backend):
    """Pins a deliberate consequence: 'always see their own' means a demoted Admin keeps reading
    what THEY wrote. It does not give them anything anyone else wrote."""
    p = uuid.uuid4()
    await _memory(world.a.tenant, world.a.users[1], p, "design", "written by A1", 1)
    await _memory(world.a.tenant, world.a.users[2], p, "design", "written by someone else", 1)
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET role = 3 WHERE id = :u"), {"u": world.a.users[1]})
        await db.commit()
    got = {i["content"] for i in (await _get(world.a.token[1])).json()["items"]}
    assert got == {"written by A1"}


async def test_own_entries_are_still_narrowed_by_project_and_phase(world, backend):
    me, p1, p2 = world.a.users[2], uuid.uuid4(), uuid.uuid4()
    await _memory(world.a.tenant, me, p1, "planning", "own p1 planning", 1)
    await _memory(world.a.tenant, me, p1, "testing", "own p1 testing", 1)
    await _memory(world.a.tenant, me, p2, "planning", "own p2 planning", 1)
    names = lambda r: {i["content"] for i in r.json()["items"]}  # noqa: E731
    tok = world.a.token[2]
    assert names(await _get(tok, project_id=str(p1))) == {"own p1 planning", "own p1 testing"}
    assert names(await _get(tok, project_id=str(p1), phase="planning", include_earlier_phases="false")) == {"own p1 planning"}
    assert names(await _get(tok, project_id=str(uuid.uuid4()))) == set()


# ================================================================================================
# GET: clearance, tenant, filters, paging
# ================================================================================================
@pytest.mark.parametrize("role,expected", [(1, {"s", "d", "o"}), (2, {"d", "o"}), (3, {"o"})])
async def test_list_obeys_clearance(world, backend, role, expected):
    p, a = uuid.uuid4(), world.a.users[1]
    for text_, level in (("s", 1), ("d", 2), ("o", 3)):
        await _memory(world.a.tenant, a, p, "design", text_, level)
    r = await _get(world.a.token[role])
    assert {i["content"] for i in r.json()["items"]} == expected
    assert r.json()["total"] == len(expected)


async def test_list_never_shows_another_tenants_entries(world, backend):
    p = uuid.uuid4()
    await _memory(world.b.tenant, world.b.users[1], p, "design", "theirs", 3)
    await _memory(world.a.tenant, world.a.users[1], p, "design", "ours", 3)
    r = await _get(world.a.token[1], project_id=str(p))
    assert [i["content"] for i in r.json()["items"]] == ["ours"] and r.json()["total"] == 1


async def test_list_uses_the_current_role_not_the_tokens(world, backend):
    # Written by the Developer, not by the Admin who is about to be demoted: authorship (ADR-0023)
    # must not be what this test is measuring.
    await _memory(world.a.tenant, world.a.users[2], uuid.uuid4(), "design", "admin only", 1)
    stale_admin = world.a.token[1]
    assert len((await _get(stale_admin)).json()["items"]) == 1
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET role = 3 WHERE id = :u"), {"u": world.a.users[1]})
        await db.commit()
    assert (await _get(stale_admin)).json()["items"] == []


async def test_list_filters_by_project_and_phase_with_the_same_rules_as_retrieve(world, backend):
    p1, p2, a = uuid.uuid4(), uuid.uuid4(), world.a.users[1]
    for phase in ("planning", "analysis", "design", "testing"):
        await _memory(world.a.tenant, a, p1, phase, f"p1-{phase}", 3)
    await _memory(world.a.tenant, a, p2, "design", "p2-design", 3)
    tok = world.a.token[1]
    names = lambda r: {i["content"] for i in r.json()["items"]}  # noqa: E731
    assert names(await _get(tok, project_id=str(p1))) == {"p1-planning", "p1-analysis", "p1-design", "p1-testing"}
    assert names(await _get(tok, project_id=str(p1), phase="design")) == {"p1-planning", "p1-analysis", "p1-design"}
    assert names(await _get(tok, project_id=str(p1), phase="design", include_earlier_phases="false")) == {"p1-design"}
    assert names(await _get(tok, phase="design", include_earlier_phases="false")) == {"p1-design", "p2-design"}


async def test_list_paging_total_and_newest_first_order(world, backend):
    p, a = uuid.uuid4(), world.a.users[1]
    for i in range(5):
        await _memory(world.a.tenant, a, p, "design", f"note-{i}", 3)
    tok = world.a.token[1]
    full = (await _get(tok, limit=50)).json()
    assert full["total"] == 5 and len(full["items"]) == 5
    created = [i["created_at"] for i in full["items"]]
    assert created == sorted(created, reverse=True), "newest first"
    page1 = (await _get(tok, limit=2, offset=0)).json()
    page2 = (await _get(tok, limit=2, offset=2)).json()
    page3 = (await _get(tok, limit=2, offset=4)).json()
    assert (len(page1["items"]), len(page2["items"]), len(page3["items"])) == (2, 2, 1)
    assert page1["total"] == page2["total"] == page3["total"] == 5
    ids = [i["id"] for p_ in (page1, page2, page3) for i in p_["items"]]
    assert len(set(ids)) == 5, "pages do not overlap"


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"phase": "maintenance"},
                                    {"project_id": "x"}])
async def test_list_rejects_bad_parameters(world, backend, params):
    assert (await _get(world.a.token[1], **params)).status_code == 422


async def test_list_requires_authentication_and_never_returns_vectors(world, backend):
    await _memory(world.a.tenant, world.a.users[1], uuid.uuid4(), "design", "x", 3)
    assert (await _get(None)).status_code == 401
    item = (await _get(world.a.token[3])).json()["items"][0]
    assert "embedding" not in item and "tenant_id" not in item


# ================================================================================================
# PATCH / DELETE (ADR-0025): Admin only, same tenant only
# ================================================================================================
async def _patch(token, memory_id, **body):
    async with await _client() as c:
        return await c.patch(f"/project-memory/{memory_id}", json=body, headers=_auth(token) if token else {})


async def _delete(token, memory_id):
    async with await _client() as c:
        return await c.delete(f"/project-memory/{memory_id}", headers=_auth(token) if token else {})


async def _row(memory_id):
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(ProjectMemory).where(ProjectMemory.id == memory_id))).scalar_one_or_none()


async def test_an_admin_can_reclassify_an_entry_and_visibility_changes_immediately(world, backend):
    mid = await _memory(world.a.tenant, world.a.users[2], uuid.uuid4(), "design", "decision note", 1, vec=bow("decision"))
    assert (await _get(world.a.token[3])).json()["items"] == []                  # Admin-only: a Trainee sees nothing

    r = await _patch(world.a.token[1], mid, min_role_required=3)
    assert r.status_code == 200 and r.json()["min_role_required"] == 3
    assert [i["id"] for i in (await _get(world.a.token[3])).json()["items"]] == [str(mid)]

    assert (await _patch(world.a.token[1], mid, min_role_required=1)).status_code == 200
    assert (await _get(world.a.token[3])).json()["items"] == []                  # and back


async def test_reclassifying_changes_only_the_level(world, backend):
    p = uuid.uuid4()
    mid = await _memory(world.a.tenant, world.a.users[2], p, "testing", "keep me intact", 1)
    before = await _row(mid)
    await _patch(world.a.token[1], mid, min_role_required=2)
    after = await _row(mid)
    assert after.min_role_required == 2
    for field in ("content", "project_id", "sdlc_phase", "created_by_user_id", "created_at", "tenant_id", "embedding_model"):
        assert getattr(after, field) == getattr(before, field), field
    assert list(after.embedding) == list(before.embedding)


@pytest.mark.parametrize("who", [2, 3])
async def test_developers_and_trainees_cannot_edit_or_delete_not_even_their_own_entry(world, backend, who):
    mid = await _memory(world.a.tenant, world.a.users[who], uuid.uuid4(), "design", "mine", 1)
    assert (await _patch(world.a.token[who], mid, min_role_required=3)).status_code == 401
    assert (await _delete(world.a.token[who], mid)).status_code == 401
    row = await _row(mid)
    assert row is not None and row.min_role_required == 1


async def test_no_token_and_operator_token_are_rejected(world, backend):
    from app.security import create_operator_token
    mid = await _memory(world.a.tenant, world.a.users[1], uuid.uuid4(), "design", "x", 1)
    for token in (None, "garbage", create_operator_token(uuid.uuid4())):
        assert (await _patch(token, mid, min_role_required=3)).status_code == 401
        assert (await _delete(token, mid)).status_code == 401
    assert await _row(mid) is not None


async def test_an_admin_cannot_touch_another_tenants_entry_and_gets_the_same_answer_as_for_a_missing_id(world, backend):
    theirs = await _memory(world.b.tenant, world.b.users[1], uuid.uuid4(), "design", "tenant B secret", 1)
    r_other = await _patch(world.a.token[1], theirs, min_role_required=3)
    r_gone = await _patch(world.a.token[1], uuid.uuid4(), min_role_required=3)
    assert r_other.status_code == r_gone.status_code == 404
    assert r_other.json() == r_gone.json(), "must not reveal whether the id exists in another tenant"
    d_other, d_gone = await _delete(world.a.token[1], theirs), await _delete(world.a.token[1], uuid.uuid4())
    assert d_other.status_code == d_gone.status_code == 404 and d_other.json() == d_gone.json()
    survivor = await _row(theirs)
    assert survivor is not None and survivor.min_role_required == 1, "tenant B's entry is untouched"


@pytest.mark.parametrize("body", [{"min_role_required": 0}, {"min_role_required": 4}, {"min_role_required": "admin"},
                                  {"min_role_required": None}, {}])
async def test_patch_rejects_invalid_levels_and_changes_nothing(world, backend, body):
    mid = await _memory(world.a.tenant, world.a.users[1], uuid.uuid4(), "design", "x", 2)
    assert (await _patch(world.a.token[1], mid, **body)).status_code == 422
    assert (await _row(mid)).min_role_required == 2


async def test_a_malformed_id_is_rejected(world, backend):
    assert (await _patch(world.a.token[1], "not-a-uuid", min_role_required=3)).status_code == 422
    assert (await _delete(world.a.token[1], "not-a-uuid")).status_code == 422


async def test_delete_removes_the_entry_everywhere_and_leaves_the_others(world, backend):
    p, a = uuid.uuid4(), world.a.users[1]
    gone = await _memory(world.a.tenant, a, p, "design", "delete me decision", 3, vec=bow("decision"))
    kept = await _memory(world.a.tenant, a, p, "design", "keep me decision", 3, vec=bow("decision"))
    r = await _delete(world.a.token[1], gone)
    assert r.status_code == 204 and r.content == b""
    assert await _row(gone) is None and await _row(kept) is not None
    assert [i["id"] for i in (await _get(world.a.token[1])).json()["items"]] == [str(kept)]
    from tests.test_retrieve_endpoint import _retrieve
    found = (await _retrieve(world.a.token[3], query="decision", k=20)).json()["memory"]
    assert [m["memory_id"] for m in found] == [str(kept)]
    assert (await _delete(world.a.token[1], gone)).status_code == 404            # second delete: not found


async def test_a_demoted_admins_old_token_cannot_edit_or_delete(world, backend):
    mid = await _memory(world.a.tenant, world.a.users[2], uuid.uuid4(), "design", "x", 1)
    token = world.a.token[1]
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET role = 3 WHERE id = :u"), {"u": world.a.users[1]})
        await db.commit()
    assert (await _patch(token, mid, min_role_required=3)).status_code == 401
    assert (await _delete(token, mid)).status_code == 401
    assert (await _row(mid)).min_role_required == 1


async def test_a_database_failure_during_delete_is_a_safe_500_and_the_entry_survives(world, backend, monkeypatch):
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession
    mid = await _memory(world.a.tenant, world.a.users[1], uuid.uuid4(), "design", "x", 1)

    async def boom(self, *a, **k):
        raise DBAPIError("INSERT secret_table", {}, Exception("connection reset: host=db user=guardrail"))
    monkeypatch.setattr(AsyncSession, "commit", boom)
    r = await _delete(world.a.token[1], mid)
    monkeypatch.undo()
    assert r.status_code == 500 and r.json()["detail"]["error"] == "server_error"
    assert "secret_table" not in r.text and "guardrail" not in r.text
    assert await _row(mid) is not None
