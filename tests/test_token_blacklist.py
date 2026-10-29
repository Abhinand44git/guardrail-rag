"""
Token blacklist / POST /auth/logout (Session 7). Phase 2's own build-phase
description promised "token blacklist" as part of Security core; it was
never built -- found by auditing the original Phase 2 deliverable directly,
confirmed absent at every later phase too, fixed here. End-to-end over real
HTTP: mint a token, hit a genuinely protected route to confirm it works,
call POST /auth/logout, then hit the exact same route with the exact same
token again and confirm it's now rejected -- the actual behavior the gap
was about, not just that a DB row gets written.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError
from httpx import ASGITransport, AsyncClient
from jose import jwt as jose_jwt

from app.config import settings
from app.database import AsyncSessionLocal
import app.deps as deps_module
from app.deps import purge_expired_blacklist, require_operator, require_user
from app.main import app
from app.models import Role, Tenant, TokenBlacklist, User
from app.security import create_operator_token, create_user_token, decode_token, hash_password

PASSWORD = "SomeUsersPassword123!"


async def _make_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name=f"blacklist-test-{uuid.uuid4().hex[:8]}"))
        await db.commit()
    return tenant_id


async def _make_user(tenant_id: uuid.UUID, role: int) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(
            id=user_id, tenant_id=tenant_id, username=f"u-{user_id.hex[:8]}",
            email=f"{user_id.hex[:8]}@blacklist-test.local",
            hashed_password=hash_password(PASSWORD), role=role,
        ))
        await db.commit()
    return user_id


async def _cleanup(*tenant_ids: uuid.UUID):
    async with AsyncSessionLocal() as db:
        for tenant_id in tenant_ids:
            for row in (await db.execute(
                sa.select(TokenBlacklist).where(TokenBlacklist.tenant_id == tenant_id)
            )).scalars().all():
                await db.delete(row)
            for user in (await db.execute(
                sa.select(User).where(User.tenant_id == tenant_id)
            )).scalars().all():
                await db.delete(user)
            tenant = await db.get(Tenant, tenant_id)
            if tenant:
                await db.delete(tenant)
        await db.commit()


# --- end-to-end: user plane ---------------------------------------------

async def test_logout_blacklists_user_token_and_subsequent_request_is_rejected():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = create_user_token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            headers = {"Authorization": f"Bearer {token}"}

            # Sanity: works before logout, so the 401 after really is the
            # blacklist and not something else.
            before = await client.get("/admin/users", headers=headers)
            assert before.status_code == 200, before.text

            logout = await client.post("/auth/logout", headers=headers)
            assert logout.status_code == 204, logout.text

            after = await client.get("/admin/users", headers=headers)
        assert after.status_code == 401
    finally:
        await _cleanup(tenant_id)


async def test_logout_is_idempotent():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = create_user_token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            headers = {"Authorization": f"Bearer {token}"}
            first = await client.post("/auth/logout", headers=headers)
            second = await client.post("/auth/logout", headers=headers)
        assert first.status_code == 204
        assert second.status_code == 204, "calling logout twice must not error"
    finally:
        await _cleanup(tenant_id)


async def test_blacklisting_one_token_does_not_affect_another_valid_token():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token_a = create_user_token(admin_id, tenant_id, Role.ADMIN.value)
    token_b = create_user_token(admin_id, tenant_id, Role.ADMIN.value)  # same user, second login/session
    try:
        async with await _make_client() as client:
            await client.post("/auth/logout", headers={"Authorization": f"Bearer {token_a}"})
            still_good = await client.get("/admin/users", headers={"Authorization": f"Bearer {token_b}"})
        assert still_good.status_code == 200, (
            "blacklisting one session's token must not blacklist a different token for the same user"
        )
    finally:
        await _cleanup(tenant_id)


async def test_blacklisted_user_token_row_has_correct_tenant_and_expiry():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = create_user_token(admin_id, tenant_id, Role.ADMIN.value)
    claims = decode_token(token)
    try:
        async with await _make_client() as client:
            r = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 204

        async with AsyncSessionLocal() as db:
            row = await db.get(TokenBlacklist, claims["jti"])
        assert row is not None
        assert row.tenant_id == tenant_id
        expected_expiry = datetime.fromtimestamp(claims["exp"], tz=timezone.utc)
        assert abs((row.expires_at - expected_expiry).total_seconds()) < 2
    finally:
        await _cleanup(tenant_id)


# --- end-to-end: operator plane -----------------------------------------

async def test_logout_blacklists_operator_token_and_subsequent_request_is_rejected():
    """Session 9: operator logout moved to its own POST /operator/logout --
    see the plane-separation tests below for /auth/logout now rejecting an
    operator token outright instead of accepting it."""
    operator_id = uuid.uuid4()
    tenant_id = await _make_tenant()  # a real tenant to PATCH against
    token = create_operator_token(operator_id)
    try:
        async with await _make_client() as client:
            headers = {"Authorization": f"Bearer {token}"}

            before = await client.patch(f"/operator/tenants/{tenant_id}", json={"is_active": True}, headers=headers)
            assert before.status_code == 200, before.text

            logout = await client.post("/operator/logout", headers=headers)
            assert logout.status_code == 204, logout.text

            after = await client.patch(f"/operator/tenants/{tenant_id}", json={"is_active": True}, headers=headers)
        assert after.status_code == 401
    finally:
        await _cleanup(tenant_id)


async def test_blacklisted_operator_token_row_has_null_tenant_id():
    token = create_operator_token(uuid.uuid4())
    claims = decode_token(token)
    async with await _make_client() as client:
        r = await client.post("/operator/logout", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204

    async with AsyncSessionLocal() as db:
        row = await db.get(TokenBlacklist, claims["jti"])
        assert row is not None
        assert row.tenant_id is None
        await db.delete(row)
        await db.commit()


# --- plane separation (Session 9) ----------------------------------------

async def test_auth_logout_rejects_an_operator_token():
    """The thing Session 7's single plane-agnostic /auth/logout explicitly
    did NOT do -- now it does, matching every other route's plane
    separation (app/deps.py::require_user_plane_logout)."""
    token = create_operator_token(uuid.uuid4())
    async with await _make_client() as client:
        r = await client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401

    # And confirm it genuinely wasn't blacklisted as a side effect of the
    # rejected attempt -- the operator's token should still work normally.
    claims = decode_token(token)
    async with AsyncSessionLocal() as db:
        assert await db.get(TokenBlacklist, claims["jti"]) is None


async def test_operator_logout_rejects_a_user_token():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = create_user_token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post("/operator/logout", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401

        claims = decode_token(token)
        async with AsyncSessionLocal() as db:
            assert await db.get(TokenBlacklist, claims["jti"]) is None
    finally:
        await _cleanup(tenant_id)


# --- edge cases -----------------------------------------------------------

async def test_logout_without_a_token_is_rejected():
    async with await _make_client() as client:
        r = await client.post("/auth/logout")
    assert r.status_code == 401


async def test_logout_on_a_pre_migration_token_with_no_jti_is_a_graceful_no_op():
    """A token minted before this fix (no jti claim at all) can't be
    individually blacklisted -- logout on one must not 500, and must not
    write a row under a null/empty key that could later collide with or
    mask something else. Uses a user-plane token specifically, since
    Session 9 split logout by plane -- an operator-plane token here would
    be rejected on the plane check alone, before ever reaching the jti
    handling this test is actually about."""
    no_jti_token = jose_jwt.encode(
        {
            "sub": str(uuid.uuid4()), "plane": "user",
            "tenant_id": str(uuid.uuid4()), "role": Role.ADMIN.value,
            "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
        },
        settings.jwt_secret_key, algorithm=settings.jwt_algorithm,
    )
    async with await _make_client() as client:
        r = await client.post("/auth/logout", headers={"Authorization": f"Bearer {no_jti_token}"})
    assert r.status_code == 204

    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sa.select(TokenBlacklist).where(
            sa.or_(TokenBlacklist.token_jti.is_(None), TokenBlacklist.token_jti == "")
        ))).scalars().all()
    assert rows == []


# --- dependency-level (mirrors tests/test_jwt_planes.py's style) --------

async def test_require_user_rejects_blacklisted_token_directly(db_session):
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, name=f"dep-test-{uuid.uuid4().hex[:6]}"))
    await db_session.flush()
    user_id = uuid.uuid4()
    db_session.add(User(
        id=user_id, tenant_id=tenant_id, username=f"u-{user_id.hex[:8]}",
        email=f"{user_id.hex[:8]}@dep-test.local", hashed_password=hash_password("x"),
        role=Role.ADMIN.value,
    ))
    await db_session.flush()

    token = create_user_token(user_id, tenant_id, Role.ADMIN.value)
    claims = decode_token(token)
    db_session.add(TokenBlacklist(
        token_jti=claims["jti"], tenant_id=tenant_id,
        expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
    ))
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await require_user(token, db_session)
    assert exc.value.status_code == 401


async def test_require_operator_rejects_blacklisted_token_directly(db_session):
    token = create_operator_token(uuid.uuid4())
    claims = decode_token(token)
    db_session.add(TokenBlacklist(
        token_jti=claims["jti"], tenant_id=None,
        expires_at=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
    ))
    await db_session.flush()

    with pytest.raises(HTTPException) as exc:
        await require_operator(token, db_session)
    assert exc.value.status_code == 401


# --- purge of expired rows (ADR-0018): housekeeping, never a requirement ------

def _row(jti: str, *, expired: bool) -> TokenBlacklist:
    delta = timedelta(hours=-1) if expired else timedelta(hours=1)
    return TokenBlacklist(token_jti=jti, tenant_id=None, expires_at=datetime.now(timezone.utc) + delta)


async def _present(db, *jtis: str) -> set[str]:
    rows = await db.execute(sa.select(TokenBlacklist.token_jti).where(TokenBlacklist.token_jti.in_(jtis)))
    return set(rows.scalars().all())


async def test_purge_removes_expired_rows_and_keeps_live_ones(db_session):
    await purge_expired_blacklist(db_session, limit=100_000)  # start from a known-clean state
    old, live = f"old-{uuid.uuid4()}"[:36], f"live-{uuid.uuid4()}"[:36]
    db_session.add_all([_row(old, expired=True), _row(live, expired=False)])
    await db_session.flush()

    removed = await purge_expired_blacklist(db_session)

    assert removed >= 1
    assert await _present(db_session, old, live) == {live}, "only the expired row may go; a live revocation must stay"
    await db_session.execute(sa.delete(TokenBlacklist).where(TokenBlacklist.token_jti == live))


async def test_purge_is_bounded_per_call(db_session):
    await purge_expired_blacklist(db_session, limit=100_000)
    jtis = [f"b{i}-{uuid.uuid4()}"[:36] for i in range(5)]
    db_session.add_all([_row(j, expired=True) for j in jtis])
    await db_session.flush()

    removed = await purge_expired_blacklist(db_session, limit=2)

    assert removed == 2
    assert len(await _present(db_session, *jtis)) == 3, "the rest are left for later logouts, not one giant DELETE"
    await purge_expired_blacklist(db_session, limit=100_000)


async def test_logout_purges_expired_rows_but_still_revokes_the_token():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = create_user_token(admin_id, tenant_id, Role.ADMIN.value)
    stale = f"stale-{uuid.uuid4()}"[:36]
    async with AsyncSessionLocal() as db:
        db.add(_row(stale, expired=True))
        await db.commit()
    try:
        async with await _make_client() as client:
            headers = {"Authorization": f"Bearer {token}"}
            assert (await client.post("/auth/logout", headers=headers)).status_code == 204
            after = await client.get("/admin/users", headers=headers)
        async with AsyncSessionLocal() as db:
            assert await _present(db, stale) == set(), "an expired row should be gone after any logout"
        assert after.status_code == 401, "and the token that just logged out must still be revoked"
    finally:
        await _cleanup(tenant_id)


async def test_a_failing_purge_never_makes_logout_fail_or_unrevoke(monkeypatch):
    async def _boom(*args, **kwargs):
        raise DBAPIError("DELETE ...", {}, Exception("simulated purge failure"))

    monkeypatch.setattr(deps_module, "purge_expired_blacklist", _boom)
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = create_user_token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            headers = {"Authorization": f"Bearer {token}"}
            logout = await client.post("/auth/logout", headers=headers)
            after = await client.get("/admin/users", headers=headers)
        assert logout.status_code == 204, logout.text
        assert after.status_code == 401, "housekeeping failure must not undo the revocation"
    finally:
        await _cleanup(tenant_id)
