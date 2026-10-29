"""
ADR-0024: every authenticated endpoint uses the caller's CURRENT role from the database, not the
role frozen into their login token. Before this, a demoted Admin kept Admin powers on /admin/* and
/ingest/* until the token expired (up to 60 minutes).

Each test uses a token whose role claim DISAGREES with the database (or changes the database after
the token was issued), then calls an endpoint that existed before Phase 5. If require_user ever goes
back to trusting the token, these fail.
"""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.database import AsyncSessionLocal
from app.main import app
from app.models import Tenant
from app.security import create_user_token
from tests.test_retrieve_endpoint import _auth, _chunk, _make_user, _purge, backend  # noqa: F401


@pytest.fixture
async def tenant():
    tid = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tid, name=f"role-test-{tid.hex[:8]}", max_admins=3))
        await db.commit()
    yield tid
    await _purge(tid)


async def _call(method, path, token, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, headers=_auth(token), **kw)


async def _set_role(user_id, role):
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET role = :r WHERE id = :u"), {"r": role, "u": user_id})
        await db.commit()


async def test_a_token_claiming_admin_for_a_trainee_cannot_use_admin_endpoints(tenant):
    trainee = await _make_user(tenant, 3)
    forged_claim = create_user_token(trainee, tenant, 1)  # signed and valid, but the claim is stale/wrong
    assert (await _call("GET", "/admin/users", forged_claim)).status_code == 401
    assert (await _call("POST", "/admin/users", forged_claim,
                        json={"username": "x", "email": "x@y.local", "role": 3})).status_code == 401


async def test_a_demoted_admin_loses_admin_powers_on_the_very_next_request(tenant):
    admin = await _make_user(tenant, 1)
    token = create_user_token(admin, tenant, 1)
    assert (await _call("GET", "/admin/users", token)).status_code == 200
    await _set_role(admin, 3)  # demoted after the token was issued
    assert (await _call("GET", "/admin/users", token)).status_code == 401


async def test_demotion_through_the_real_endpoint_by_another_admin_takes_effect_immediately(tenant):
    """The end-to-end version: Admin B demotes Admin A through PATCH /admin/users/{id}; A's existing
    token stops working on the next request. (Two Admins need max_admins >= 2: set in the fixture.)"""
    a, b = await _make_user(tenant, 1), await _make_user(tenant, 1)
    token_a, token_b = create_user_token(a, tenant, 1), create_user_token(b, tenant, 1)
    assert (await _call("GET", "/admin/users", token_a)).status_code == 200
    r = await _call("PATCH", f"/admin/users/{a}", token_b, json={"role": 3})
    assert r.status_code == 200, r.text
    assert (await _call("GET", "/admin/users", token_a)).status_code == 401


async def test_a_demoted_developer_can_no_longer_ingest(tenant, backend):
    dev = await _make_user(tenant, 2)
    token = create_user_token(dev, tenant, 2)
    body = {"title": "t", "content": "def f():\n    return 1\n"}
    assert (await _call("POST", "/ingest/code", token, json=body)).status_code == 201
    await _set_role(dev, 3)
    assert (await _call("POST", "/ingest/code", token, json=body)).status_code == 401


async def test_a_demoted_admin_can_no_longer_reclassify_or_delete_documents(tenant, backend):
    admin = await _make_user(tenant, 1)
    token = create_user_token(admin, tenant, 1)
    await _chunk(tenant, "doc", "some text", 1)
    async with AsyncSessionLocal() as db:
        doc_id = (await db.execute(text("SELECT id FROM documents WHERE tenant_id = :t"), {"t": tenant})).scalar_one()
    await _set_role(admin, 2)
    assert (await _call("PATCH", f"/ingest/document/{doc_id}", token, json={"min_role_required": 3})).status_code == 401
    assert (await _call("DELETE", f"/ingest/document/{doc_id}", token)).status_code == 401


async def test_a_promotion_also_takes_effect_immediately_without_logging_in_again(tenant, backend):
    person = await _make_user(tenant, 3)
    token = create_user_token(person, tenant, 3)  # logged in as a Trainee
    body = {"title": "t", "content": "x = 1\n"}
    assert (await _call("POST", "/ingest/code", token, json=body)).status_code == 401
    await _set_role(person, 2)
    assert (await _call("POST", "/ingest/code", token, json=body)).status_code == 201


async def test_a_token_whose_tenant_differs_from_the_users_tenant_is_refused_on_older_endpoints_too(tenant):
    other = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=other, name=f"role-test-other-{other.hex[:8]}"))
        await db.commit()
    try:
        admin = await _make_user(tenant, 1)
        mismatched = create_user_token(admin, other, 1)
        assert (await _call("GET", "/admin/users", mismatched)).status_code == 401
    finally:
        await _purge(other)


async def test_a_deactivated_user_is_still_refused_and_distinguishably(tenant):
    """Unchanged behaviour, pinned so the new return path cannot hide it."""
    admin = await _make_user(tenant, 1)
    token = create_user_token(admin, tenant, 1)
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": admin})
        await db.commit()
    assert (await _call("GET", "/admin/users", token)).status_code == 401
