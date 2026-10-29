"""
Regression test for the "Known fixes from Phase 1" LoginRequest.email entry:
the decision log documented switching from EmailStr to plain str, but the
shipped code still had EmailStr, so every seeded fixture login
(admin@acme-corp.local etc.) was actually returning 422, not authenticating.
This hits the real /auth/login endpoint over ASGI (not just the schema in
isolation) so the fix is proven end-to-end.

Operator login now lives at /operator/login (Phase 3 naming-drift fix, see
app/routers/operator.py) -- tested there in tests/test_operator_endpoints.py
alongside the rest of the operator-plane routes, not here.

Uses its own committed rows (not the db_session savepoint fixture) because
the app's request-scoped session, opened via app.database.get_db, is a
different DB connection than the test's -- a savepoint on one connection
isn't visible to the other.
"""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from app.database import AsyncSessionLocal
from app.main import app
from app.models import Operator, Role, Tenant, User
from app.security import decode_token, hash_password

PASSWORD = "ChangeMe123!"


async def test_user_login_with_local_domain_email_succeeds():
    # Unique per test run, not the literal seeded "admin@acme-corp.local":
    # since users.email is now unique (migration 0003), reusing that exact
    # address would collide with the seed script's row. Still a .local
    # domain, which is the actual thing under test.
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    email = f"admin-{uuid.uuid4().hex[:8]}@acme-corp.local"
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name=f"regression-test-{uuid.uuid4().hex[:8]}"))
        await db.flush()
        db.add(User(
            id=user_id, tenant_id=tenant_id, username="admin",
            email=email, hashed_password=hash_password(PASSWORD),
            role=Role.ADMIN.value,
        ))
        await db.commit()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post(
                "/auth/login",
                json={"email": email, "password": PASSWORD},
            )

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["plane"] == "user"
        payload = decode_token(body["access_token"])
        assert payload["tenant_id"] == str(tenant_id)
        assert payload["role"] == Role.ADMIN.value
    finally:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            if user:
                await db.delete(user)
            tenant = await db.get(Tenant, tenant_id)
            if tenant:
                await db.delete(tenant)
            await db.commit()


async def test_login_with_wrong_password_returns_generic_401():
    """Same error either way (no such account / wrong password) -- response
    shape must not leak which one it was."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/auth/login",
            json={"email": "no-such-user@nowhere.local", "password": "wrong"},
        )
    assert r.status_code == 401
    assert r.json() == {"detail": {"error": "unauthorized", "detail": "invalid email or password"}}


async def test_deactivated_user_login_rejected_with_specific_error():
    """Phase 3.5: DELETE /admin/users sets is_active=false. A correct
    password must not be enough to log in afterward -- distinct error from
    wrong-password, since the credential itself is proven valid at this
    point (see app/routers/auth.py::_ACCOUNT_DEACTIVATED)."""
    tenant_id, user_id = uuid.uuid4(), uuid.uuid4()
    email = f"deactivated-{uuid.uuid4().hex[:8]}@acme-corp.local"
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name=f"deactivated-login-test-{uuid.uuid4().hex[:8]}"))
        await db.flush()
        db.add(User(
            id=user_id, tenant_id=tenant_id, username="deactivated-user",
            email=email, hashed_password=hash_password(PASSWORD),
            role=Role.DEVELOPER.value, is_active=False,
        ))
        await db.commit()

    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 401
        assert r.json()["detail"]["error"] == "unauthorized"
        assert "deactivated" in r.json()["detail"]["detail"]
    finally:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, user_id)
            if user:
                await db.delete(user)
            tenant = await db.get(Tenant, tenant_id)
            if tenant:
                await db.delete(tenant)
            await db.commit()


async def test_old_auth_operator_login_path_is_gone():
    """The pre-Phase-3 path is removed, not aliased -- see decision log
    naming-drift note. A request to the old path must 404, not silently
    still work."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/auth/operator/login",
            json={"email": "anyone@guardrail.local", "password": "whatever"},
        )
    assert r.status_code == 404
