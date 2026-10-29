"""
Phase 3.5: admin-plane user management (/admin/users), tested end-to-end
over ASGI against the real Postgres + pgvector instance -- same priority as
test_operator_endpoints.py: this is role-escalation code (an Admin creating
and promoting other Admins), so it gets proven working, not just written.

Setup pattern: most tests insert a committed Tenant + Admin User row
directly (AsyncSessionLocal) and mint the Admin's bearer token directly via
create_user_token, the same shortcut test_jwt_planes.py and test_rbac.py
already use -- the real /auth/login and /operator/.../admin flows are
covered by their own dedicated test files, so re-deriving them here would
just be redundant setup, not additional coverage. One exception:
test_full_chain_operator_to_admin_to_new_user below goes through the real
Operator -> first-Admin-login -> /admin/users chain once, end to end, to
prove the pieces actually connect in practice, not just in isolation.
"""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app import email as email_module
from app.database import AsyncSessionLocal
from app.main import app
from app.models import Operator, Role, Tenant, User
from app.security import create_user_token, hash_password, verify_password

PASSWORD = "SomeUsersPassword123!"


async def _make_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_tenant(max_admins: int = 1) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name=f"admin-test-{uuid.uuid4().hex[:8]}", max_admins=max_admins))
        await db.commit()
    return tenant_id


async def _make_user(tenant_id: uuid.UUID, role: int, is_active: bool = True) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(
            id=user_id, tenant_id=tenant_id, username=f"u-{user_id.hex[:8]}",
            email=f"{user_id.hex[:8]}@admin-endpoint-test.local",
            hashed_password=hash_password(PASSWORD), role=role, is_active=is_active,
        ))
        await db.commit()
    return user_id


def _admin_token(user_id: uuid.UUID, tenant_id: uuid.UUID) -> str:
    return create_user_token(user_id, tenant_id, Role.ADMIN.value)


async def _cleanup(tenant_id: uuid.UUID | None, operator_id: uuid.UUID | None = None):
    async with AsyncSessionLocal() as db:
        if tenant_id:
            result = await db.execute(User.__table__.select().where(User.tenant_id == tenant_id))
            for row in result.fetchall():
                u = await db.get(User, row.id)
                if u:
                    await db.delete(u)
            t = await db.get(Tenant, tenant_id)
            if t:
                await db.delete(t)
        if operator_id:
            op = await db.get(Operator, operator_id)
            if op:
                await db.delete(op)
        await db.commit()


async def _get_user_row(user_id: uuid.UUID) -> User | None:
    async with AsyncSessionLocal() as db:
        return await db.get(User, user_id)


# --- POST /admin/users ------------------------------------------------


async def test_create_developer_gets_emailed_password_not_in_response():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        new_email = f"dev-{uuid.uuid4().hex[:8]}@admin-endpoint-test.local"
        outbox_before = len(email_module.dev_outbox)
        async with await _make_client() as client:
            r = await client.post(
                "/admin/users",
                json={"username": "new-dev", "email": new_email, "role": Role.DEVELOPER.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["role"] == Role.DEVELOPER.value
        assert body["is_active"] is True
        assert "password" not in body and "hashed_password" not in body

        assert len(email_module.dev_outbox) == outbox_before + 1
        sent = email_module.dev_outbox[-1]
        assert sent.to == new_email
        assert "Developer" in sent.subject
        temp_password = sent.body.split("Temporary password: ")[1].split("\n")[0]

        user = await _get_user_row(uuid.UUID(body["id"]))
        assert verify_password(temp_password, user.hashed_password)
    finally:
        await _cleanup(tenant_id)


async def test_create_admin_via_admin_users_respects_tenant_cap():
    """Cap=2: admin1 (bootstrap) + admin2 (created here) succeeds; admin3
    must be rejected. Proves POST /admin/users' cap check is parameterized
    by tenant.max_admins, not hardcoded to 1 like the Operator's endpoint."""
    tenant_id = await _make_tenant(max_admins=2)
    admin1_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _admin_token(admin1_id, tenant_id)
    try:
        async with await _make_client() as client:
            second = await client.post(
                "/admin/users",
                json={"username": "admin2", "email": f"a2-{uuid.uuid4().hex[:8]}@admin-endpoint-test.local", "role": Role.ADMIN.value},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert second.status_code == 201, second.text

            third = await client.post(
                "/admin/users",
                json={"username": "admin3", "email": f"a3-{uuid.uuid4().hex[:8]}@admin-endpoint-test.local", "role": Role.ADMIN.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert third.status_code == 400
        assert "maximum" in third.json()["detail"]["detail"].lower()
    finally:
        await _cleanup(tenant_id)


async def test_create_user_rejects_non_admin_caller():
    tenant_id = await _make_tenant()
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value)
    token = create_user_token(dev_id, tenant_id, Role.DEVELOPER.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/admin/users",
                json={"username": "sneaky", "email": "sneaky@admin-endpoint-test.local", "role": Role.TRAINEE.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 401
    finally:
        await _cleanup(tenant_id)


async def test_create_user_rejects_username_collision_within_tenant():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    await _make_user(tenant_id, Role.TRAINEE.value)  # unrelated existing user
    token = _admin_token(admin_id, tenant_id)
    try:
        async with AsyncSessionLocal() as db:
            db.add(User(
                id=uuid.uuid4(), tenant_id=tenant_id, username="taken",
                email=f"taken-{uuid.uuid4().hex[:8]}@admin-endpoint-test.local",
                hashed_password=hash_password(PASSWORD), role=Role.TRAINEE.value,
            ))
            await db.commit()

        async with await _make_client() as client:
            r = await client.post(
                "/admin/users",
                json={"username": "taken", "email": "whoever@admin-endpoint-test.local", "role": Role.TRAINEE.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert "username" in r.json()["detail"]["detail"].lower()
    finally:
        await _cleanup(tenant_id)


# --- GET /admin/users ----------------------------------------------------


async def test_list_users_filters_by_role_and_paginates():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    for _ in range(3):
        await _make_user(tenant_id, Role.DEVELOPER.value)
    await _make_user(tenant_id, Role.TRAINEE.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            all_r = await client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
            dev_r = await client.get(
                "/admin/users", params={"role": Role.DEVELOPER.value},
                headers={"Authorization": f"Bearer {token}"},
            )
            page_r = await client.get(
                "/admin/users", params={"limit": 2, "offset": 0},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert all_r.status_code == 200
        assert all_r.json()["total"] == 5  # admin + 3 devs + 1 trainee

        assert dev_r.status_code == 200
        dev_body = dev_r.json()
        assert dev_body["total"] == 3
        assert all(u["role"] == Role.DEVELOPER.value for u in dev_body["items"])

        assert page_r.status_code == 200
        page_body = page_r.json()
        assert len(page_body["items"]) == 2
        assert page_body["total"] == 5
    finally:
        await _cleanup(tenant_id)


async def test_list_users_is_scoped_to_callers_own_tenant():
    tenant_a = await _make_tenant()
    tenant_b = await _make_tenant()
    admin_a = await _make_user(tenant_a, Role.ADMIN.value)
    await _make_user(tenant_b, Role.DEVELOPER.value)
    token = _admin_token(admin_a, tenant_a)
    try:
        async with await _make_client() as client:
            r = await client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1  # only the caller's own tenant's Admin
        assert all(u["tenant_id"] == str(tenant_a) for u in body["items"])
    finally:
        await _cleanup(tenant_a)
        await _cleanup(tenant_b)


# --- PATCH /admin/users/{id} ----------------------------------------------


async def test_patch_promotes_trainee_to_developer():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    trainee_id = await _make_user(tenant_id, Role.TRAINEE.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{trainee_id}",
                json={"role": Role.DEVELOPER.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["role"] == Role.DEVELOPER.value
    finally:
        await _cleanup(tenant_id)


async def test_patch_promotion_to_admin_blocked_by_cap_then_succeeds_after_freeing_slot():
    """cap=2, two active Admins already fill it: promoting a third person
    to Admin is blocked. Deactivating one of the two existing Admins frees
    a slot (1 active out of a cap of 2), and the same promotion then
    succeeds -- the actual point of the trigger update in migration 0008
    and the app-level check mirroring it in admin.py.

    Needs cap=2, not 1: with cap=1 there can only ever be one active Admin
    at a time, so there would be no *second* Admin available to act as
    caller once the first is deactivated -- and a sole Admin can't target
    themselves (self-target guard, see test_patch_self_target_rejected)."""
    tenant_id = await _make_tenant(max_admins=2)
    admin1_id = await _make_user(tenant_id, Role.ADMIN.value)
    admin2_id = await _make_user(tenant_id, Role.ADMIN.value)  # fills the cap alongside admin1
    trainee_id = await _make_user(tenant_id, Role.TRAINEE.value)
    token = _admin_token(admin2_id, tenant_id)
    try:
        async with await _make_client() as client:
            blocked = await client.patch(
                f"/admin/users/{trainee_id}",
                json={"role": Role.ADMIN.value},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert blocked.status_code == 400
            assert "maximum" in blocked.json()["detail"]["detail"].lower() or "admin" in blocked.json()["detail"]["detail"].lower()

            deactivate = await client.patch(
                f"/admin/users/{admin1_id}",
                json={"is_active": False},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert deactivate.status_code == 200

            promote = await client.patch(
                f"/admin/users/{trainee_id}",
                json={"role": Role.ADMIN.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert promote.status_code == 200, promote.text
        assert promote.json()["role"] == Role.ADMIN.value
    finally:
        await _cleanup(tenant_id)


async def test_patch_reactivating_admin_blocked_by_cap():
    """cap=1, admin1 deactivated, trainee promoted to Admin to fill the
    slot (as above) -- reactivating admin1 now would push the tenant to 2
    active Admins against a cap of 1, and must be rejected. confirm_email
    is supplied and correct here -- this test is specifically about the
    cap check, not the reactivation-confirmation check (that's covered
    separately below)."""
    tenant_id = await _make_tenant(max_admins=1)
    admin1_id = await _make_user(tenant_id, Role.ADMIN.value, is_active=False)
    admin2_id = await _make_user(tenant_id, Role.ADMIN.value)  # fills the one slot
    token = _admin_token(admin2_id, tenant_id)
    try:
        admin1_email = (await _get_user_row(admin1_id)).email
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{admin1_id}",
                json={"is_active": True, "confirm_email": admin1_email},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert "maximum" in r.json()["detail"]["detail"].lower() or "admin" in r.json()["detail"]["detail"].lower()
    finally:
        await _cleanup(tenant_id)


# --- PATCH /admin/users/{id} — reactivation email confirmation -----------


async def test_patch_reactivation_succeeds_with_correct_confirm_email():
    tenant_id = await _make_tenant(max_admins=2)
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value, is_active=False)
    token = _admin_token(admin_id, tenant_id)
    try:
        dev_email = (await _get_user_row(dev_id)).email
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{dev_id}",
                json={"is_active": True, "confirm_email": dev_email},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is True
    finally:
        await _cleanup(tenant_id)


async def test_patch_reactivation_rejected_with_wrong_confirm_email():
    """Proves the check actually compares against the target row's real
    email, not just checking that *some* string was supplied."""
    tenant_id = await _make_tenant(max_admins=2)
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value, is_active=False)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{dev_id}",
                json={"is_active": True, "confirm_email": "definitely-not-their-email@wrong.local"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert "confirm_email" in r.json()["detail"]["detail"]

        untouched = await _get_user_row(dev_id)
        assert untouched.is_active is False  # rejected before any mutation
    finally:
        await _cleanup(tenant_id)


async def test_patch_reactivation_rejected_when_confirm_email_missing():
    tenant_id = await _make_tenant(max_admins=2)
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value, is_active=False)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{dev_id}",
                json={"is_active": True},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert "confirm_email" in r.json()["detail"]["detail"]
    finally:
        await _cleanup(tenant_id)


async def test_patch_confirm_email_not_required_for_deactivation():
    """The confirmation only guards the restore-access direction. Turning
    someone off needs no extra step -- consistent with DELETE (also a
    deactivation) needing none either."""
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value, is_active=True)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{dev_id}",
                json={"is_active": False},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is False
    finally:
        await _cleanup(tenant_id)


async def test_patch_confirm_email_not_required_for_plain_role_change():
    """A role-only PATCH never touches is_active, so it's not a
    reactivation and needs no confirmation, regardless of the target's
    current active status."""
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    trainee_id = await _make_user(tenant_id, Role.TRAINEE.value, is_active=True)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{trainee_id}",
                json={"role": Role.DEVELOPER.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
    finally:
        await _cleanup(tenant_id)


async def test_patch_confirm_email_not_required_when_already_active():
    """Re-sending is_active: true on a user who's already active isn't a
    real reactivation transition (no false->true flip), so no confirmation
    should be demanded."""
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value, is_active=True)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{dev_id}",
                json={"is_active": True},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
    finally:
        await _cleanup(tenant_id)


async def test_patch_self_target_rejected():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{admin_id}",
                json={"is_active": False},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert "own" in r.json()["detail"]["detail"].lower()
    finally:
        await _cleanup(tenant_id)


async def test_patch_missing_user_returns_generic_404():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{uuid.uuid4()}",
                json={"role": Role.DEVELOPER.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
        assert r.json()["detail"] == {"error": "not_found", "detail": "user not found"}
    finally:
        await _cleanup(tenant_id)


async def test_patch_cannot_reach_another_tenants_user():
    """An Admin targeting a real user id that belongs to a DIFFERENT tenant
    must get the same generic 404 as a nonexistent id -- never a "found but
    wrong tenant" style error that would leak cross-tenant structure."""
    tenant_a = await _make_tenant()
    tenant_b = await _make_tenant()
    admin_a = await _make_user(tenant_a, Role.ADMIN.value)
    other_tenants_user = await _make_user(tenant_b, Role.TRAINEE.value)
    token = _admin_token(admin_a, tenant_a)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{other_tenants_user}",
                json={"role": Role.DEVELOPER.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
        assert r.json()["detail"] == {"error": "not_found", "detail": "user not found"}

        # Prove it truly wasn't touched, not just that the response hid it.
        untouched = await _get_user_row(other_tenants_user)
        assert untouched.role == Role.TRAINEE.value
    finally:
        await _cleanup(tenant_a)
        await _cleanup(tenant_b)


async def test_patch_empty_body_rejected():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    trainee_id = await _make_user(tenant_id, Role.TRAINEE.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.patch(
                f"/admin/users/{trainee_id}", json={},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 422
    finally:
        await _cleanup(tenant_id)


# --- DELETE /admin/users/{id} — soft deactivation -------------------------


async def test_delete_soft_deactivates_not_hard_deletes():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.delete(f"/admin/users/{dev_id}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        assert r.json()["is_active"] is False

        row = await _get_user_row(dev_id)
        assert row is not None  # still a real row -- soft delete, not hard delete
        assert row.is_active is False
    finally:
        await _cleanup(tenant_id)


async def test_delete_frees_admin_cap_slot_for_new_admin_creation():
    """cap=2, two active Admins already fill it (same reasoning as the
    promotion test above for why cap must be 2, not 1, to have a second
    Admin available as caller). Deactivating one frees a slot; creating a
    fresh Admin afterward fills it back up to the cap and succeeds."""
    tenant_id = await _make_tenant(max_admins=2)
    admin1_id = await _make_user(tenant_id, Role.ADMIN.value)
    admin2_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _admin_token(admin2_id, tenant_id)
    try:
        async with await _make_client() as client:
            deactivate = await client.delete(f"/admin/users/{admin1_id}", headers={"Authorization": f"Bearer {token}"})
            assert deactivate.status_code == 200

            create = await client.post(
                "/admin/users",
                json={"username": "new-admin", "email": f"na-{uuid.uuid4().hex[:8]}@admin-endpoint-test.local", "role": Role.ADMIN.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert create.status_code == 201, create.text
    finally:
        await _cleanup(tenant_id)


async def test_delete_self_target_rejected():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _admin_token(admin_id, tenant_id)
    try:
        async with await _make_client() as client:
            r = await client.delete(f"/admin/users/{admin_id}", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400
        assert "own" in r.json()["detail"]["detail"].lower()

        row = await _get_user_row(admin_id)
        assert row.is_active is True  # untouched
    finally:
        await _cleanup(tenant_id)


async def test_deactivated_user_immediately_loses_access_and_cannot_login():
    """The two enforcement sides this phase adds: an already-issued token
    stops working immediately (app/deps.py::require_user), and a fresh
    login attempt with the correct password is rejected too
    (app/routers/auth.py). Both proven at the real HTTP layer here, not
    just at the dependency layer (that's tests/test_jwt_planes.py and
    tests/test_auth_endpoints.py respectively)."""
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value)
    admin_token = _admin_token(admin_id, tenant_id)
    dev_token = create_user_token(dev_id, tenant_id, Role.DEVELOPER.value)
    dev_email = (await _get_user_row(dev_id)).email
    try:
        async with await _make_client() as client:
            deactivate = await client.delete(f"/admin/users/{dev_id}", headers={"Authorization": f"Bearer {admin_token}"})
            assert deactivate.status_code == 200

            still_using_old_token = await client.get(
                "/admin/users", headers={"Authorization": f"Bearer {dev_token}"}
            )
            assert still_using_old_token.status_code == 401

            login_attempt = await client.post(
                "/auth/login", json={"email": dev_email, "password": PASSWORD}
            )
        assert login_attempt.status_code == 401
        assert "deactivated" in login_attempt.json()["detail"]["detail"]
    finally:
        await _cleanup(tenant_id)


async def test_get_users_requires_admin_role():
    tenant_id = await _make_tenant()
    trainee_id = await _make_user(tenant_id, Role.TRAINEE.value)
    token = create_user_token(trainee_id, tenant_id, Role.TRAINEE.value)
    try:
        async with await _make_client() as client:
            r = await client.get("/admin/users", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
    finally:
        await _cleanup(tenant_id)


# --- Full chain: Operator -> first Admin login -> /admin/users -----------


async def test_full_chain_operator_to_admin_to_new_user():
    """One end-to-end proof that Phase 3's Operator flow and Phase 3.5's
    Admin flow actually connect: a real Operator creates a real tenant and
    first Admin, that Admin logs in for real (not a minted token), and
    then uses /admin/users to create a Developer."""
    operator_id = uuid.uuid4()
    op_email = f"op-{uuid.uuid4().hex[:8]}@guardrail.local"
    op_password = "OperatorChain123!"
    async with AsyncSessionLocal() as db:
        db.add(Operator(
            id=operator_id, username=f"op-{uuid.uuid4().hex[:8]}",
            email=op_email, hashed_password=hash_password(op_password),
        ))
        await db.commit()

    tenant_id = None
    try:
        async with await _make_client() as client:
            op_login = await client.post("/operator/login", json={"email": op_email, "password": op_password})
            op_token = op_login.json()["access_token"]

            create_tenant = await client.post(
                "/operator/tenants",
                json={"name": f"Full Chain Test {uuid.uuid4().hex[:6]}", "max_admins": 1},
                headers={"Authorization": f"Bearer {op_token}"},
            )
            tenant_id = uuid.UUID(create_tenant.json()["id"])

            admin_email = f"chain-admin-{uuid.uuid4().hex[:8]}@fullchaintest.local"
            create_admin = await client.post(
                f"/operator/tenants/{tenant_id}/admin",
                json={"username": "chain-admin", "email": admin_email},
                headers={"Authorization": f"Bearer {op_token}"},
            )
            admin_temp_password = email_module.dev_outbox[-1].body.split("Temporary password: ")[1].split("\n")[0]

            admin_login = await client.post(
                "/auth/login", json={"email": admin_email, "password": admin_temp_password}
            )
            assert admin_login.status_code == 200, admin_login.text
            admin_token = admin_login.json()["access_token"]

            new_dev_email = f"chain-dev-{uuid.uuid4().hex[:8]}@fullchaintest.local"
            create_dev = await client.post(
                "/admin/users",
                json={"username": "chain-dev", "email": new_dev_email, "role": Role.DEVELOPER.value},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        assert create_dev.status_code == 201, create_dev.text
        assert create_dev.json()["tenant_id"] == str(tenant_id)
    finally:
        await _cleanup(tenant_id, operator_id)
