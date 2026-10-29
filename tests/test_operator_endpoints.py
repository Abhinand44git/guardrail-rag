"""
Phase 3: operator-plane endpoints, tested end-to-end over ASGI against the
real Postgres + pgvector instance (not mocked) -- per the project's stated
priority that RBAC/auth/plane-separation code be proven working, not just
written. This is the highest-consequence code in the system alongside the
RBAC query itself: it's what creates tenants and grants the very first
Admin credential for each one.

Uses its own committed rows via AsyncSessionLocal, same pattern as
test_auth_endpoints.py -- the app's request-scoped session (app.database.get_db)
is a different DB connection than a test-local db_session savepoint, so
fixture setup here goes through committed inserts + explicit teardown.
"""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app import email as email_module
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Operator, Role, Tenant, User
from app.security import create_user_token, decode_token, hash_password, verify_password

OP_PASSWORD = "OperatorPass123!"


async def _make_client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


async def _make_operator() -> tuple[uuid.UUID, str]:
    """Creates a committed Operator row and returns (id, bearer_token)."""
    operator_id = uuid.uuid4()
    email = f"op-{uuid.uuid4().hex[:8]}@guardrail.local"
    async with AsyncSessionLocal() as db:
        db.add(Operator(
            id=operator_id, username=f"op-{uuid.uuid4().hex[:8]}",
            email=email, hashed_password=hash_password(OP_PASSWORD),
        ))
        await db.commit()

    async with await _make_client() as client:
        r = await client.post("/operator/login", json={"email": email, "password": OP_PASSWORD})
    assert r.status_code == 200, r.text
    return operator_id, r.json()["access_token"]


async def _cleanup_tenant(tenant_id: uuid.UUID | None, operator_id: uuid.UUID | None = None):
    async with AsyncSessionLocal() as db:
        if tenant_id:
            result = await db.execute(User.__table__.select().where(User.tenant_id == tenant_id))
            for row in result.fetchall():
                user = await db.get(User, row.id)
                if user:
                    await db.delete(user)
            tenant = await db.get(Tenant, tenant_id)
            if tenant:
                await db.delete(tenant)
        if operator_id:
            op = await db.get(Operator, operator_id)
            if op:
                await db.delete(op)
        await db.commit()


# --- /operator/login (moved from /auth/operator/login) --------------------


async def test_operator_login_succeeds_and_token_has_no_tenant_claim():
    operator_id, token = await _make_operator()
    payload = decode_token(token)
    assert payload["plane"] == "operator"
    assert "tenant_id" not in payload
    assert "role" not in payload
    await _cleanup_tenant(None, operator_id)


# --- POST /operator/tenants ------------------------------------------------


async def test_create_tenant_has_no_ai_model_configured():
    """The actual bug this whole prep session fixed, proven at the HTTP
    layer: a tenant created through the real endpoint gets no default
    generation model -- not Groq, not anything."""
    operator_id, token = await _make_operator()
    tenant_id = None
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/operator/tenants",
                json={"name": f"Test Co {uuid.uuid4().hex[:6]}", "max_admins": 2},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        tenant_id = uuid.UUID(body["id"])
        assert body["is_active"] is True
        assert body["max_admins"] == 2
        # AI config fields must not even appear in the operator-facing
        # response -- decision log item #5, lifecycle vs config separation.
        assert "llm_provider" not in body
        assert "llm_api_key_ref" not in body

        async with AsyncSessionLocal() as db:
            tenant = await db.get(Tenant, tenant_id)
            assert tenant.llm_provider is None
            assert tenant.llm_model_name is None
            assert tenant.llm_api_key_ref is None
            assert tenant.created_by_operator_id == operator_id
    finally:
        await _cleanup_tenant(tenant_id, operator_id)


async def test_create_tenant_rejects_user_plane_token():
    """A tenant-scoped user token must not be able to create tenants --
    this is the operator/user plane boundary, tested from the tenant side
    this time (test_jwt_planes.py already tests it from the dependency
    layer directly; this proves it holds at the real route too)."""
    fake_tenant_id, fake_user_id = uuid.uuid4(), uuid.uuid4()
    user_token = create_user_token(fake_user_id, fake_tenant_id, Role.ADMIN.value)
    async with await _make_client() as client:
        r = await client.post(
            "/operator/tenants",
            json={"name": "Should Not Be Created"},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert r.status_code == 401


async def test_create_tenant_requires_auth():
    async with await _make_client() as client:
        r = await client.post("/operator/tenants", json={"name": "No Token"})
    assert r.status_code == 401


# --- POST /operator/tenants/{id}/admin -------------------------------------


async def test_create_first_admin_password_never_in_response_but_works_via_email():
    operator_id, token = await _make_operator()
    tenant_id = None
    try:
        async with await _make_client() as client:
            create_r = await client.post(
                "/operator/tenants",
                json={"name": f"Admin Test {uuid.uuid4().hex[:6]}", "max_admins": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
            tenant_id = uuid.UUID(create_r.json()["id"])

            admin_email = f"admin-{uuid.uuid4().hex[:8]}@admintest.local"
            outbox_before = len(email_module.dev_outbox)
            admin_r = await client.post(
                f"/operator/tenants/{tenant_id}/admin",
                json={"username": "admin", "email": admin_email},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert admin_r.status_code == 201, admin_r.text
        body = admin_r.json()
        assert body["email"] == admin_email
        assert body["role"] == Role.ADMIN.value
        assert body["tenant_id"] == str(tenant_id)
        # The whole point of emailing it: never in the API response body.
        assert "password" not in body
        assert "hashed_password" not in body

        # Prove the emailed password is the *real*, working credential --
        # not just that some password was set. Dev-mode outbox stands in
        # for a real inbox (see app/email.py docstring); this sandbox has
        # no SMTP egress anyway, so this is the honest way to prove the
        # send-then-usable contract without a live mail server.
        assert len(email_module.dev_outbox) == outbox_before + 1
        sent = email_module.dev_outbox[-1]
        assert sent.to == admin_email
        temp_password = sent.body.split("Temporary password: ")[1].split("\n")[0]

        async with AsyncSessionLocal() as db:
            user = await db.get(User, uuid.UUID(body["id"]))
            assert verify_password(temp_password, user.hashed_password)
    finally:
        await _cleanup_tenant(tenant_id, operator_id)


async def test_create_second_admin_via_operator_is_rejected_first_admin_only():
    """Locked this session: the Operator's endpoint only ever creates a
    tenant's FIRST Admin. Uses max_admins=2 deliberately -- there's plenty
    of room under the cap, so a rejection here proves it's the new
    first-admin-only rule firing, not the (unrelated, still-real) cap."""
    operator_id, token = await _make_operator()
    tenant_id = None
    try:
        async with await _make_client() as client:
            create_r = await client.post(
                "/operator/tenants",
                json={"name": f"First Admin Only Test {uuid.uuid4().hex[:6]}", "max_admins": 2},
                headers={"Authorization": f"Bearer {token}"},
            )
            tenant_id = uuid.UUID(create_r.json()["id"])

            first = await client.post(
                f"/operator/tenants/{tenant_id}/admin",
                json={"username": "admin1", "email": f"a1-{uuid.uuid4().hex[:6]}@firstadmintest.local"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert first.status_code == 201, first.text

            second = await client.post(
                f"/operator/tenants/{tenant_id}/admin",
                json={"username": "admin2", "email": f"a2-{uuid.uuid4().hex[:6]}@firstadmintest.local"},
                headers={"Authorization": f"Bearer {token}"},
            )

        assert second.status_code == 400
        body = second.json()["detail"]
        assert body["error"] == "invalid_input"
        assert "already has an Admin" in body["detail"]
        # Safe error contract: no tenant id, no raw Postgres exception text.
        assert str(tenant_id) not in body["detail"]

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                User.__table__.select().where(User.tenant_id == tenant_id)
            )
            assert len(result.fetchall()) == 1  # second admin truly never landed
    finally:
        await _cleanup_tenant(tenant_id, operator_id)


async def test_create_admin_for_missing_tenant_returns_generic_404():
    operator_id, token = await _make_operator()
    try:
        async with await _make_client() as client:
            r = await client.post(
                f"/operator/tenants/{uuid.uuid4()}/admin",
                json={"username": "ghost", "email": "ghost@nowhere.local"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
        assert r.json()["detail"] == {"error": "not_found", "detail": "tenant not found"}
    finally:
        await _cleanup_tenant(None, operator_id)


async def test_first_admin_username_collision_with_existing_user_rejected():
    """First-admin-only means this endpoint can't be called twice for one
    tenant anymore (see test above), so the natural "duplicate username"
    scenario is now: some other user already exists in this tenant (e.g.
    a Developer/Trainee created via Phase 3.5's future /admin/users) with
    the same username the Operator is about to use for the first Admin.
    Inserted directly here since Phase 3.5 doesn't exist yet to create it
    the real way -- this proves the per-tenant uniqueness constraint (and
    its pre-check) is still reachable and correct, not dead code."""
    operator_id, token = await _make_operator()
    tenant_id = None
    try:
        async with await _make_client() as client:
            create_r = await client.post(
                "/operator/tenants",
                json={"name": f"Username Test {uuid.uuid4().hex[:6]}"},
                headers={"Authorization": f"Bearer {token}"},
            )
            tenant_id = uuid.UUID(create_r.json()["id"])

            async with AsyncSessionLocal() as db:
                db.add(User(
                    id=uuid.uuid4(), tenant_id=tenant_id, username="admin",
                    email=f"pre-{uuid.uuid4().hex[:6]}@unametest.local",
                    hashed_password=hash_password("Whatever123!"),
                    role=Role.DEVELOPER.value,
                ))
                await db.commit()

            r = await client.post(
                f"/operator/tenants/{tenant_id}/admin",
                json={"username": "admin", "email": f"admin-{uuid.uuid4().hex[:6]}@unametest.local"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_input"
        assert "username" in r.json()["detail"]["detail"].lower()
    finally:
        await _cleanup_tenant(tenant_id, operator_id)


async def test_same_username_allowed_across_different_tenants():
    """Proves the actual point of scoping the constraint to (tenant_id,
    username) rather than username alone: two unrelated companies can both
    have an admin literally named 'admin', no naming-convention workaround
    required."""
    operator_id, token = await _make_operator()
    tenant_a_id = tenant_b_id = None
    try:
        async with await _make_client() as client:
            a_r = await client.post(
                "/operator/tenants",
                json={"name": f"Company A {uuid.uuid4().hex[:6]}"},
                headers={"Authorization": f"Bearer {token}"},
            )
            b_r = await client.post(
                "/operator/tenants",
                json={"name": f"Company B {uuid.uuid4().hex[:6]}"},
                headers={"Authorization": f"Bearer {token}"},
            )
            tenant_a_id = uuid.UUID(a_r.json()["id"])
            tenant_b_id = uuid.UUID(b_r.json()["id"])

            admin_a = await client.post(
                f"/operator/tenants/{tenant_a_id}/admin",
                json={"username": "admin", "email": f"a-{uuid.uuid4().hex[:6]}@companya.local"},
                headers={"Authorization": f"Bearer {token}"},
            )
            admin_b = await client.post(
                f"/operator/tenants/{tenant_b_id}/admin",
                json={"username": "admin", "email": f"b-{uuid.uuid4().hex[:6]}@companyb.local"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert admin_a.status_code == 201, admin_a.text
        assert admin_b.status_code == 201, admin_b.text
    finally:
        await _cleanup_tenant(tenant_a_id, None)
        await _cleanup_tenant(tenant_b_id, operator_id)


# --- PATCH /operator/tenants/{id} — lifecycle + grandfather behavior ------


async def test_patch_tenant_deactivate_and_reactivate():
    operator_id, token = await _make_operator()
    tenant_id = None
    try:
        async with await _make_client() as client:
            create_r = await client.post(
                "/operator/tenants",
                json={"name": f"Lifecycle Test {uuid.uuid4().hex[:6]}"},
                headers={"Authorization": f"Bearer {token}"},
            )
            tenant_id = uuid.UUID(create_r.json()["id"])

            deactivate_r = await client.patch(
                f"/operator/tenants/{tenant_id}",
                json={"is_active": False},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert deactivate_r.status_code == 200
            assert deactivate_r.json()["is_active"] is False

            reactivate_r = await client.patch(
                f"/operator/tenants/{tenant_id}",
                json={"is_active": True},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert reactivate_r.status_code == 200
            assert reactivate_r.json()["is_active"] is True
    finally:
        await _cleanup_tenant(tenant_id, operator_id)


async def test_patch_tenant_empty_body_rejected():
    operator_id, token = await _make_operator()
    tenant_id = None
    try:
        async with await _make_client() as client:
            create_r = await client.post(
                "/operator/tenants",
                json={"name": f"Empty Patch Test {uuid.uuid4().hex[:6]}"},
                headers={"Authorization": f"Bearer {token}"},
            )
            tenant_id = uuid.UUID(create_r.json()["id"])

            r = await client.patch(
                f"/operator/tenants/{tenant_id}",
                json={},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 422
    finally:
        await _cleanup_tenant(tenant_id, operator_id)


async def test_lowering_max_admins_grandfathers_existing_admins():
    """The specific behavior decision log Phase 3 calls out for a dedicated
    test: lowering max_admins does NOT remove or demote existing Admins --
    the trigger only fires on INSERT. Admin #1 comes from the real Operator
    endpoint; admin #2 is inserted directly (the endpoint is first-admin-
    only now, so a real second Admin would come from Phase 3.5's future
    /admin/users, which doesn't exist yet -- this simulates that state).
    Cap starts at 2, lowered to 1: both existing Admins must survive, and a
    third Admin must be rejected."""
    operator_id, token = await _make_operator()
    tenant_id = None
    try:
        async with await _make_client() as client:
            create_r = await client.post(
                "/operator/tenants",
                json={"name": f"Grandfather Test {uuid.uuid4().hex[:6]}", "max_admins": 2},
                headers={"Authorization": f"Bearer {token}"},
            )
            tenant_id = uuid.UUID(create_r.json()["id"])

            admin1_r = await client.post(
                f"/operator/tenants/{tenant_id}/admin",
                json={"username": "admin1", "email": f"g1-{uuid.uuid4().hex[:6]}@grandtest.local"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert admin1_r.status_code == 201, admin1_r.text
            admin1_id = uuid.UUID(admin1_r.json()["id"])

            admin2_id = uuid.uuid4()
            async with AsyncSessionLocal() as db:
                db.add(User(
                    id=admin2_id, tenant_id=tenant_id, username="admin2",
                    email=f"g2-{uuid.uuid4().hex[:6]}@grandtest.local",
                    hashed_password=hash_password("Whatever123!"),
                    role=Role.ADMIN.value,
                ))
                await db.commit()

            patch_r = await client.patch(
                f"/operator/tenants/{tenant_id}",
                json={"max_admins": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert patch_r.status_code == 200
            assert patch_r.json()["max_admins"] == 1

            # Both pre-existing admins must still be there, untouched.
            async with AsyncSessionLocal() as db:
                still_there = [await db.get(User, admin1_id), await db.get(User, admin2_id)]
                assert all(u is not None for u in still_there)
                assert all(u.role == Role.ADMIN.value for u in still_there)

            # A third admin, now over the lowered cap, must be rejected --
            # via the operator endpoint this would actually be rejected for
            # "already has an Admin" (first-admin-only) rather than the cap,
            # since the endpoint no longer distinguishes; either way, the
            # DB-level cap enforcement itself is proven directly below.
            async with AsyncSessionLocal() as db:
                db.add(User(
                    id=uuid.uuid4(), tenant_id=tenant_id, username="admin3",
                    email=f"g3-{uuid.uuid4().hex[:6]}@grandtest.local",
                    hashed_password=hash_password("Whatever123!"),
                    role=Role.ADMIN.value,
                ))
                with pytest.raises(Exception):
                    await db.commit()
    finally:
        await _cleanup_tenant(tenant_id, operator_id)


async def test_patch_tenant_rejects_user_plane_token():
    fake_tenant_id, fake_user_id = uuid.uuid4(), uuid.uuid4()
    user_token = create_user_token(fake_user_id, fake_tenant_id, Role.ADMIN.value)
    async with await _make_client() as client:
        r = await client.patch(
            f"/operator/tenants/{uuid.uuid4()}",
            json={"is_active": False},
            headers={"Authorization": f"Bearer {user_token}"},
        )
    assert r.status_code == 401


async def test_patch_missing_tenant_returns_generic_404():
    operator_id, token = await _make_operator()
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/operator/tenants/{uuid.uuid4()}",
                json={"is_active": False},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
        assert r.json()["detail"] == {"error": "not_found", "detail": "tenant not found"}
    finally:
        await _cleanup_tenant(None, operator_id)
