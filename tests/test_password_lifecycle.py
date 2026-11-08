"""
Batch C (ADR-0026): temporary passwords, forced change, expiry, reissue, session revocation.

Written as attacks, against the real Postgres over ASGI:
  * default-deny is proved by WALKING THE WHOLE ROUTE TABLE, not by testing a few routes, so a
    future endpoint that forgets the rule fails here without anyone remembering to add a test;
  * old sessions must die when a password changes (they are the thing an attacker holds);
  * the policy is tried with the inputs a lazy or hostile user would actually type.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from jose import jwt as jose_jwt
from sqlalchemy import select, text

from app import email as email_module
from app.config import settings
from app.database import AsyncSessionLocal
from app.email import EmailSendError
from app.main import app
from app.models import Operator, Role, Tenant, User
from app.password_policy import MAX_BYTES, MIN_LENGTH, check_new_password
from app.security import create_operator_token, create_user_token, hash_password, verify_password

PW = "Initial-pass-phrase-1"
NEW_PW = "Correct horse battery 91"


# ------------------------------------------------------------------ fixtures and helpers
@pytest.fixture
async def env():
    e = SimpleNamespace(tenants=[], operators=[])
    yield e
    async with AsyncSessionLocal() as db:
        for t in e.tenants:  # users, tokens: ON DELETE CASCADE
            await db.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": t})
        for o in e.operators:
            await db.execute(text("DELETE FROM operators WHERE id = :o"), {"o": o})
        await db.commit()


async def mk_tenant(env, max_admins=3) -> uuid.UUID:
    tid = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tid, name=f"lifecycle-{tid.hex[:8]}", max_admins=max_admins))
        await db.commit()
    env.tenants.append(tid)
    return tid


async def mk_user(env, tenant, role=Role.DEVELOPER.value, *, must_change=False, expires=None,
                  session_version=0, password=PW, active=True):
    uid = uuid.uuid4()
    u = SimpleNamespace(id=uid, tenant=tenant, role=role, sv=session_version, password=password,
                        username=f"user-{uid.hex[:6]}", email=f"{uid.hex[:8]}@lifecycle-test.local")
    async with AsyncSessionLocal() as db:
        db.add(User(id=uid, tenant_id=tenant, username=u.username, email=u.email,
                    hashed_password=hash_password(password), role=role, is_active=active,
                    must_change_password=must_change, temp_password_expires_at=expires,
                    session_version=session_version))
        await db.commit()
    return u


async def mk_operator(env, *, must_change=False, session_version=0, password=PW):
    oid = uuid.uuid4()
    o = SimpleNamespace(id=oid, sv=session_version, password=password,
                        username=f"op-{oid.hex[:6]}", email=f"{oid.hex[:8]}@lifecycle-op.local")
    async with AsyncSessionLocal() as db:
        db.add(Operator(id=oid, username=o.username, email=o.email, hashed_password=hash_password(password),
                        must_change_password=must_change, session_version=session_version))
        await db.commit()
    env.operators.append(oid)
    return o


def utok(u):
    return create_user_token(u.id, u.tenant, u.role, u.sv)


def otok(o):
    return create_operator_token(o.id, o.sv)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def call(method, path, token=None, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, path, headers=auth(token) if token else {}, **kw)


async def login(email, password):
    return await call("POST", "/auth/login", json={"email": email, "password": password})


async def change(token, current, new):
    return await call("POST", "/auth/change-password", token, json={"current_password": current, "new_password": new})


async def row(user_id) -> User:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(User).where(User.id == user_id))).scalar_one()


async def orow(op_id) -> Operator:
    async with AsyncSessionLocal() as db:
        return (await db.execute(select(Operator).where(Operator.id == op_id))).scalar_one()


def emailed_password() -> str:
    return email_module.dev_outbox[-1].body.split("Temporary password: ")[1].split("\n")[0]


def _walk_routes():
    """Every (method, path) in the live route table."""
    for r in app.routes:
        if isinstance(r, APIRoute):
            for m in sorted(r.methods - {"HEAD", "OPTIONS"}):
                yield m, r.path


_PUBLIC = {"/health", "/auth/login", "/operator/login"}
_USER_EXEMPT = {"/auth/change-password", "/auth/logout"}
_OPERATOR_EXEMPT = {"/operator/change-password", "/operator/logout", "/operator/login"}


def _fill(path):
    return re.sub(r"\{[^}]+\}", lambda _: str(uuid.uuid4()), path)


# ================================================================================================
# A. Default-deny: a pending password blocks EVERY route except the two exempt ones
# ================================================================================================
async def test_every_tenant_route_refuses_a_user_who_still_holds_a_temporary_password(env):
    t = await mk_tenant(env)
    pending = await mk_user(env, t, Role.ADMIN.value, must_change=True)
    token = utok(pending)
    routes = [(m, p) for m, p in _walk_routes()
              if p not in _PUBLIC and p not in _USER_EXEMPT and not p.startswith("/operator")]
    walked = {p for _, p in routes}
    # not vacuous: the walk really did reach the routes that matter
    assert {"/retrieve", "/admin/users", "/ingest/code", "/ingest/document", "/project-memory"} <= walked
    assert len(routes) >= 14

    bad = []
    for method, path in routes:
        r = await call(method, _fill(path), token, json={})
        if r.status_code != 403 or r.json()["detail"]["error"] != "password_change_required":
            bad.append((method, path, r.status_code))
    assert not bad, f"these routes did not refuse a pending-password user: {bad}"


async def test_every_operator_route_refuses_an_operator_who_still_holds_a_temporary_password(env):
    op = await mk_operator(env, must_change=True)
    token = otok(op)
    routes = [(m, p) for m, p in _walk_routes() if p.startswith("/operator") and p not in _OPERATOR_EXEMPT]
    assert {"/operator/tenants", "/operator/tenants/{tenant_id}", "/operator/tenants/{tenant_id}/admin"} <= {p for _, p in routes}
    bad = []
    for method, path in routes:
        r = await call(method, _fill(path), token, json={})
        if r.status_code != 403 or r.json()["detail"]["error"] != "password_change_required":
            bad.append((method, path, r.status_code))
    assert not bad, bad


async def test_the_same_user_is_not_refused_once_the_flag_is_clear(env):
    """Control for the walk above: the refusal is caused by the flag, not by something else."""
    t = await mk_tenant(env)
    ok = await mk_user(env, t, Role.ADMIN.value, must_change=False)
    assert (await call("GET", "/admin/users", utok(ok))).status_code == 200


async def test_logout_stays_open_to_a_user_with_a_pending_password(env):
    t = await mk_tenant(env)
    pending = await mk_user(env, t, must_change=True)
    token = utok(pending)
    assert (await call("POST", "/auth/logout", token)).status_code == 204
    assert (await call("POST", "/auth/change-password", token,
                       json={"current_password": PW, "new_password": NEW_PW})).status_code == 401, "logged out: dead"


async def test_login_tells_a_pending_user_what_to_do_and_a_normal_user_nothing_special(env):
    t = await mk_tenant(env)
    pending = await mk_user(env, t, must_change=True)
    normal = await mk_user(env, t)
    assert (await login(pending.email, PW)).json()["password_change_required"] is True
    assert (await login(normal.email, PW)).json()["password_change_required"] is False


# ================================================================================================
# B. change-password
# ================================================================================================
async def test_first_login_flow_temporary_password_to_a_real_one(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t, Role.ADMIN.value, must_change=True,
                      expires=datetime.now(timezone.utc) + timedelta(hours=10))
    temp_token = (await login(u.email, PW)).json()["access_token"]
    assert (await call("GET", "/admin/users", temp_token)).status_code == 403

    r = await change(temp_token, PW, NEW_PW)
    assert r.status_code == 200, r.text
    assert r.json()["password_change_required"] is False
    fresh = r.json()["access_token"]

    after = await row(u.id)
    assert after.must_change_password is False and after.temp_password_expires_at is None
    assert after.session_version == 1
    assert verify_password(NEW_PW, after.hashed_password) and not verify_password(PW, after.hashed_password)
    assert (await call("GET", "/admin/users", fresh)).status_code == 200, "the fresh token works"
    assert (await call("GET", "/admin/users", temp_token)).status_code == 401, "the temporary-session token is dead"
    assert (await login(u.email, PW)).status_code == 401, "the temporary password no longer logs in"
    ok = await login(u.email, NEW_PW)
    assert ok.status_code == 200 and ok.json()["password_change_required"] is False
    # ...and a token from that later login must carry the NEW session version, or it would be refused.
    assert (await call("GET", "/admin/users", ok.json()["access_token"])).status_code == 200


async def test_a_change_signs_out_every_other_session_but_keeps_the_caller_logged_in(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t, Role.ADMIN.value)
    laptop = (await login(u.email, PW)).json()["access_token"]
    phone = (await login(u.email, PW)).json()["access_token"]   # a second, stolen-looking session
    assert (await call("GET", "/admin/users", phone)).status_code == 200

    fresh = (await change(laptop, PW, NEW_PW)).json()["access_token"]
    assert (await call("GET", "/admin/users", phone)).status_code == 401
    assert (await call("GET", "/admin/users", laptop)).status_code == 401, "even the token that made the change"
    assert (await call("GET", "/admin/users", fresh)).status_code == 200


async def test_the_wrong_current_password_is_refused_and_changes_nothing(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t, must_change=True)
    token = (await login(u.email, PW)).json()["access_token"]
    r = await change(token, "not-my-password", NEW_PW)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_input"
    after = await row(u.id)
    assert verify_password(PW, after.hashed_password) and after.must_change_password is True and after.session_version == 0


@pytest.mark.parametrize("label,new", [
    ("too short", "Short1!"),
    ("eleven characters", "elevenchars"),
    ("one character short of 12", "Abcdefg1234"),
    ("a very common password", "changeme123!"),
    ("one repeated character", "aaaaaaaaaaaa"),
    ("only four different characters", "abcdabcdabcdabcd"),
    ("more than 72 bytes (ASCII)", "abcdefgh" * 10),
    ("more than 72 bytes (multi-byte)", "é" * 40),
])
async def test_weak_new_passwords_are_refused_and_nothing_changes(env, label, new):
    t = await mk_tenant(env)
    u = await mk_user(env, t)
    token = utok(u)
    r = await change(token, PW, new)
    assert r.status_code == 400, f"{label}: {r.text}"
    assert r.json()["detail"]["detail"].startswith("the new password ")
    after = await row(u.id)
    assert verify_password(PW, after.hashed_password) and after.session_version == 0
    assert (await call("GET", "/project-memory", token)).status_code == 200, "and the session is untouched"


async def test_a_new_password_equal_to_the_current_one_is_refused(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t, password="Already-a-good-one-77")
    r = await change(utok(u), "Already-a-good-one-77", "Already-a-good-one-77")
    assert r.status_code == 400 and "different" in r.json()["detail"]["detail"]


async def test_a_new_password_containing_the_username_or_the_email_name_is_refused(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t)
    local = u.email.split("@")[0]
    for new in (f"Xx-{u.username}-9999", f"{u.username.upper()}!!!!!!!!", f"zz{local}zz-71"):
        r = await change(utok(u), PW, new)
        assert r.status_code == 400 and "must not contain your" in r.json()["detail"]["detail"], new


async def test_the_length_limits_are_exact(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t)
    assert (await change(utok(u), PW, "Tr4mpoline#9")).status_code == 200      # exactly 12 characters
    u.sv, u.password = 1, "Tr4mpoline#9"
    seventy_two = "abcdefgh" * 9                                               # exactly 72 bytes
    assert (await change(utok(u), u.password, seventy_two)).status_code == 200
    u.sv, u.password = 2, seventy_two
    assert (await change(utok(u), u.password, seventy_two + "x")).status_code == 400   # 73 bytes


@pytest.mark.parametrize("body", [{}, {"current_password": PW}, {"new_password": NEW_PW},
                                  {"current_password": "", "new_password": NEW_PW},
                                  {"current_password": PW, "new_password": ""},
                                  {"current_password": PW, "new_password": "x" * 300}])
async def test_malformed_change_requests_are_rejected(env, body):
    t = await mk_tenant(env)
    u = await mk_user(env, t)
    assert (await call("POST", "/auth/change-password", utok(u), json=body)).status_code == 422
    assert verify_password(PW, (await row(u.id)).hashed_password)


async def test_change_password_needs_a_valid_user_token(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t)
    body = {"current_password": PW, "new_password": NEW_PW}
    assert (await call("POST", "/auth/change-password", None, json=body)).status_code == 401
    assert (await call("POST", "/auth/change-password", "garbage", json=body)).status_code == 401
    op = await mk_operator(env)
    assert (await call("POST", "/auth/change-password", otok(op), json=body)).status_code == 401
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET is_active = false WHERE id = :u"), {"u": u.id})
        await db.commit()
    assert (await call("POST", "/auth/change-password", utok(u), json=body)).status_code == 401
    assert verify_password(PW, (await row(u.id)).hashed_password)


# ================================================================================================
# C. session_version mechanics
# ================================================================================================
def _forge(u, **claims):
    """A correctly SIGNED token with chosen claims (what only the server can do), to test the checks."""
    base = {"sub": str(u.id), "plane": "user", "tenant_id": str(u.tenant), "role": u.role,
            "jti": str(uuid.uuid4()), "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}
    base.update(claims)
    base = {k: v for k, v in base.items() if v is not None}
    return jose_jwt.encode(base, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


async def test_a_token_from_an_older_session_version_is_refused(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t, session_version=3)
    assert (await call("GET", "/project-memory", _forge(u, sv=3))).status_code == 200
    for stale in (2, 0, -1):
        assert (await call("GET", "/project-memory", _forge(u, sv=stale))).status_code == 401


async def test_a_token_claiming_a_newer_session_version_than_the_database_is_also_refused(env):
    t = await mk_tenant(env)
    u = await mk_user(env, t, session_version=0)
    assert (await call("GET", "/project-memory", _forge(u, sv=1))).status_code == 401


async def test_a_token_with_no_sv_claim_counts_as_version_zero(env):
    """Tokens minted before this feature existed carry no sv: valid until the user's first change."""
    t = await mk_tenant(env)
    fresh = await mk_user(env, t, session_version=0)
    changed = await mk_user(env, t, session_version=1)
    assert (await call("GET", "/project-memory", _forge(fresh, sv=None))).status_code == 200
    assert (await call("GET", "/project-memory", _forge(changed, sv=None))).status_code == 401


# ================================================================================================
# D. Expiry and Admin reissue
# ================================================================================================
async def test_an_unused_temporary_password_stops_working_after_its_expiry(env):
    t = await mk_tenant(env)
    live = await mk_user(env, t, must_change=True, expires=datetime.now(timezone.utc) + timedelta(hours=1))
    dead = await mk_user(env, t, must_change=True, expires=datetime.now(timezone.utc) - timedelta(seconds=1))
    assert (await login(live.email, PW)).status_code == 200
    r = await login(dead.email, PW)
    assert r.status_code == 401 and "expired" in r.json()["detail"]["detail"]


async def test_an_expired_account_does_not_reveal_itself_to_someone_without_the_password(env):
    t = await mk_tenant(env)
    dead = await mk_user(env, t, must_change=True, expires=datetime.now(timezone.utc) - timedelta(hours=5))
    wrong = await login(dead.email, "a-wrong-guess-entirely")
    nobody = await login("nobody@lifecycle-test.local", "a-wrong-guess-entirely")
    assert wrong.status_code == nobody.status_code == 401 and wrong.json() == nobody.json()


async def test_expiry_only_applies_while_the_password_is_still_temporary(env):
    t = await mk_tenant(env)
    settled = await mk_user(env, t, must_change=False, expires=datetime.now(timezone.utc) - timedelta(days=30))
    assert (await login(settled.email, PW)).status_code == 200


async def test_an_admin_can_reissue_a_password_and_it_works_as_a_temporary_one(env):
    t = await mk_tenant(env)
    admin = await mk_user(env, t, Role.ADMIN.value)
    dev = await mk_user(env, t, Role.DEVELOPER.value, must_change=True,
                        expires=datetime.now(timezone.utc) - timedelta(hours=1))   # an expired one: locked out
    dev_old_token = utok(dev)
    assert (await login(dev.email, PW)).status_code == 401

    r = await call("POST", f"/admin/users/{dev.id}/reissue-password", utok(admin))
    assert r.status_code == 200, r.text
    assert "password" not in str(r.json()).replace("hashed_password", "").lower() or "temporary" not in r.text.lower()
    sent = email_module.dev_outbox[-1]
    assert sent.to == dev.email and "reset" in sent.subject.lower()
    new_temp = emailed_password()

    after = await row(dev.id)
    assert verify_password(new_temp, after.hashed_password) and after.must_change_password is True
    assert after.session_version == 1
    expires = after.temp_password_expires_at
    assert abs((expires - (datetime.now(timezone.utc) + timedelta(hours=settings.temp_password_ttl_hours))).total_seconds()) < 120
    assert (await call("GET", "/project-memory", dev_old_token)).status_code == 401, "their old session is signed out"
    assert (await login(dev.email, PW)).status_code == 401, "the old password is gone"
    ok = await login(dev.email, new_temp)
    assert ok.status_code == 200 and ok.json()["password_change_required"] is True


async def test_the_reissued_password_never_appears_in_the_response(env):
    t = await mk_tenant(env)
    admin, dev = await mk_user(env, t, Role.ADMIN.value), await mk_user(env, t)
    r = await call("POST", f"/admin/users/{dev.id}/reissue-password", utok(admin))
    assert emailed_password() not in r.text
    assert set(r.json()) == {"id", "tenant_id", "username", "email", "role", "is_active", "created_at"}


@pytest.mark.parametrize("who", [Role.DEVELOPER.value, Role.TRAINEE.value])
async def test_only_admins_can_reissue(env, who):
    t = await mk_tenant(env)
    actor, victim = await mk_user(env, t, who), await mk_user(env, t)
    assert (await call("POST", f"/admin/users/{victim.id}/reissue-password", utok(actor))).status_code == 401
    after = await row(victim.id)
    assert verify_password(PW, after.hashed_password) and after.session_version == 0


async def test_an_admin_cannot_reissue_their_own_password(env):
    t = await mk_tenant(env)
    admin = await mk_user(env, t, Role.ADMIN.value)
    r = await call("POST", f"/admin/users/{admin.id}/reissue-password", utok(admin))
    assert r.status_code == 400 and "change-password" in r.json()["detail"]["detail"]
    assert verify_password(PW, (await row(admin.id)).hashed_password)


async def test_reissue_for_another_tenants_user_and_for_a_missing_id_look_identical(env):
    ta, tb = await mk_tenant(env), await mk_tenant(env)
    admin_a, victim_b = await mk_user(env, ta, Role.ADMIN.value), await mk_user(env, tb)
    other = await call("POST", f"/admin/users/{victim_b.id}/reissue-password", utok(admin_a))
    gone = await call("POST", f"/admin/users/{uuid.uuid4()}/reissue-password", utok(admin_a))
    assert other.status_code == gone.status_code == 404 and other.json() == gone.json()
    after = await row(victim_b.id)
    assert verify_password(PW, after.hashed_password) and after.session_version == 0


async def test_reissue_refuses_a_deactivated_user_and_a_malformed_id_and_a_pending_admin(env):
    t = await mk_tenant(env)
    admin = await mk_user(env, t, Role.ADMIN.value)
    inactive = await mk_user(env, t, active=False)
    r = await call("POST", f"/admin/users/{inactive.id}/reissue-password", utok(admin))
    assert r.status_code == 400 and "deactivated" in r.json()["detail"]["detail"]
    assert (await call("POST", "/admin/users/not-a-uuid/reissue-password", utok(admin))).status_code == 422
    pending_admin = await mk_user(env, t, Role.ADMIN.value, must_change=True)
    victim = await mk_user(env, t)
    assert (await call("POST", f"/admin/users/{victim.id}/reissue-password", utok(pending_admin))).status_code == 403


async def test_if_the_email_cannot_be_sent_nothing_changes(env, monkeypatch):
    import app.routers.admin as admin_module
    t = await mk_tenant(env)
    admin, victim = await mk_user(env, t, Role.ADMIN.value), await mk_user(env, t)

    def boom(*a, **k):
        raise EmailSendError("smtp down")
    monkeypatch.setattr(admin_module, "send_password_reissue_email", boom)
    r = await call("POST", f"/admin/users/{victim.id}/reissue-password", utok(admin))
    assert r.status_code == 500 and r.json()["detail"]["error"] == "server_error"
    assert "smtp" not in r.text.lower()
    after = await row(victim.id)
    assert verify_password(PW, after.hashed_password) and after.session_version == 0 and after.must_change_password is False


async def test_a_demoted_admins_old_token_cannot_reissue(env):
    t = await mk_tenant(env)
    admin, victim = await mk_user(env, t, Role.ADMIN.value), await mk_user(env, t)
    token = utok(admin)
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE users SET role = 3 WHERE id = :u"), {"u": admin.id})
        await db.commit()
    assert (await call("POST", f"/admin/users/{victim.id}/reissue-password", token)).status_code == 401


# ================================================================================================
# E. Every code path that hands out a temporary password marks it as one
# ================================================================================================
async def test_an_operator_creating_a_first_admin_marks_the_password_temporary(env):
    op = await mk_operator(env)
    t = await mk_tenant(env, max_admins=1)
    email = f"first-{uuid.uuid4().hex[:8]}@lifecycle-test.local"
    r = await call("POST", f"/operator/tenants/{t}/admin", otok(op), json={"username": "first-admin", "email": email})
    assert r.status_code == 201, r.text
    created = (await row(uuid.UUID(r.json()["id"])))
    assert created.must_change_password is True
    assert abs((created.temp_password_expires_at - (datetime.now(timezone.utc) + timedelta(hours=72))).total_seconds()) < 120
    assert emailed_password() not in r.text
    assert (await login(email, emailed_password())).json()["password_change_required"] is True


async def test_an_admin_creating_a_user_marks_the_password_temporary(env):
    t = await mk_tenant(env)
    admin = await mk_user(env, t, Role.ADMIN.value)
    email = f"made-{uuid.uuid4().hex[:8]}@lifecycle-test.local"
    r = await call("POST", "/admin/users", utok(admin), json={"username": "made-user", "email": email, "role": Role.TRAINEE.value})
    assert r.status_code == 201, r.text
    created = await row(uuid.UUID(r.json()["id"]))
    assert created.must_change_password is True and created.temp_password_expires_at is not None
    assert (await login(email, emailed_password())).json()["password_change_required"] is True


async def test_the_expiry_window_follows_the_setting(env, monkeypatch):
    monkeypatch.setattr(settings, "temp_password_ttl_hours", 1)
    t = await mk_tenant(env)
    admin = await mk_user(env, t, Role.ADMIN.value)
    r = await call("POST", "/admin/users", utok(admin),
                   json={"username": "short-ttl", "email": f"s-{uuid.uuid4().hex[:8]}@lifecycle-test.local", "role": Role.TRAINEE.value})
    created = await row(uuid.UUID(r.json()["id"]))
    assert abs((created.temp_password_expires_at - (datetime.now(timezone.utc) + timedelta(hours=1))).total_seconds()) < 120


# ================================================================================================
# F. The operator plane
# ================================================================================================
async def test_operator_first_login_flow(env):
    op = await mk_operator(env, must_change=True)
    login_r = await call("POST", "/operator/login", json={"email": op.email, "password": PW})
    assert login_r.json()["password_change_required"] is True
    old = login_r.json()["access_token"]
    assert (await call("POST", "/operator/tenants", old, json={"name": "x", "max_admins": 1})).status_code == 403

    r = await call("POST", "/operator/change-password", old, json={"current_password": PW, "new_password": NEW_PW})
    assert r.status_code == 200 and r.json()["plane"] == "operator" and r.json()["password_change_required"] is False
    fresh = r.json()["access_token"]
    after = await orow(op.id)
    assert after.must_change_password is False and after.session_version == 1 and verify_password(NEW_PW, after.hashed_password)
    assert (await call("POST", "/operator/tenants", fresh, json={"name": f"op-{uuid.uuid4().hex[:6]}", "max_admins": 1})).status_code == 201
    assert (await call("POST", "/operator/tenants", old, json={"name": "x", "max_admins": 1})).status_code == 401, "older session is dead"
    # a LATER login with the new password must mint a token carrying the new session version
    again = await call("POST", "/operator/login", json={"email": op.email, "password": NEW_PW})
    assert again.status_code == 200 and again.json()["password_change_required"] is False
    later = await call("POST", "/operator/tenants", again.json()["access_token"], json={"name": f"op-{uuid.uuid4().hex[:6]}", "max_admins": 1})
    assert later.status_code == 201, later.text
    async with AsyncSessionLocal() as db:   # tidy the tenant the check above created
        await db.execute(text("DELETE FROM tenants WHERE name LIKE 'op-%' AND created_by_operator_id = :o"), {"o": op.id})
        await db.commit()


async def test_operator_wrong_current_and_weak_new_passwords_are_refused(env):
    op = await mk_operator(env)
    tok = otok(op)
    r = await call("POST", "/operator/change-password", tok, json={"current_password": "nope", "new_password": NEW_PW})
    assert r.status_code == 400
    r = await call("POST", "/operator/change-password", tok, json={"current_password": PW, "new_password": f"zz{op.username}zz-9999"})
    assert r.status_code == 400 and "username" in r.json()["detail"]["detail"]
    r = await call("POST", "/operator/change-password", tok, json={"current_password": PW, "new_password": "short"})
    assert r.status_code == 400
    after = await orow(op.id)
    assert verify_password(PW, after.hashed_password) and after.session_version == 0


async def test_the_planes_stay_separate_for_change_password(env):
    t = await mk_tenant(env)
    u, op = await mk_user(env, t), await mk_operator(env)
    body = {"current_password": PW, "new_password": NEW_PW}
    assert (await call("POST", "/operator/change-password", utok(u), json=body)).status_code == 401
    assert (await call("POST", "/auth/change-password", otok(op), json=body)).status_code == 401
    assert verify_password(PW, (await row(u.id)).hashed_password) and verify_password(PW, (await orow(op.id)).hashed_password)


async def test_an_operator_token_for_an_operator_that_does_not_exist_is_refused(env):
    """ADR-0026 tightening: operator tokens are checked against the operators table now."""
    ghost = create_operator_token(uuid.uuid4())
    assert (await call("POST", "/operator/tenants", ghost, json={"name": "x", "max_admins": 1})).status_code == 401


async def test_operator_logout_stays_open_while_a_password_change_is_pending(env):
    op = await mk_operator(env, must_change=True)
    assert (await call("POST", "/operator/logout", otok(op))).status_code == 204


# ================================================================================================
# G. The policy on its own (pure function, no database)
# ================================================================================================
def _check(new, **kw):
    kw.setdefault("current", "old-password-value")
    kw.setdefault("username", "alice-smith")
    kw.setdefault("email", "alice.smith@example.com")
    return check_new_password(new, **kw)


def test_a_reasonable_passphrase_passes():
    assert _check("purple-lantern-river-42") == []
    assert _check("Correct horse battery 91") == []


def test_no_composition_rules_a_long_lowercase_phrase_is_fine():
    assert _check("just some lowercase words here") == []


def test_each_rule_reports_its_own_problem():
    assert any("at least" in p for p in _check("short"))
    assert any("bytes" in p for p in _check("é" * 40))
    assert any("different from your current" in p for p in _check("old-password-value", current="old-password-value"))
    assert any("username" in p for p in _check("my-alice-smith-pass-1"))
    assert any("email name" in p for p in _check("xx-alice.smith-pass-1"))
    assert any("common" in p for p in _check("Password12345"))
    assert any("different characters" in p for p in _check("abababababab"))


def test_short_names_are_not_used_as_forbidden_words():
    """A 2-letter username must not ban every password containing those letters."""
    assert _check("a-perfectly-fine-passphrase", username="al", email="al@example.com") == []


def test_boundaries():
    assert MIN_LENGTH == 12 and MAX_BYTES == 72
    assert _check("abcdefgh" * 9) == []
    assert any("bytes" in p for p in _check("abcdefgh" * 9 + "i"))
