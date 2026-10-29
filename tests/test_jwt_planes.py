"""
Phase 2, decision log: JWT plane separation test.

Verifies the structural guarantee the whole operator/tenant split rests on:
operator tokens never carry a tenant_id claim, so they fail the plane check
before any route or role logic runs -- operators cannot reach /query,
/ingest, or any tenant-scoped endpoint no matter what role-check code
exists there. Tests the dependency functions directly (they're the exact
callables FastAPI's Depends() wraps), plus JWT-level attack scenarios a
"security-first" project should demonstrably defend against.

require_user became async + DB-backed in Phase 3 (checks tenant.is_active,
see app/deps.py) -- tests that call it directly now need a real committed
Tenant row and a db session, via the same db_session savepoint fixture
tests/test_rbac.py already uses.

Phase 3.5 extended the same DB-backed check to the user side (checks
user.is_active, now that the column exists) -- tests that call require_user
directly now also need a real committed User row matching the token's
`sub` claim, not just an arbitrary uuid.uuid4(). This is the same
"mechanical fix, not a design change" Session 3 already did once for the
tenant-side version of this check; see _user() below.

Session 7: require_operator also became async + DB-backed (it now checks
the token blacklist too -- see app/deps.py), so the two tests that called
it directly as a sync function now await it with a db_session, same
mechanical-fix pattern as require_user's Phase 3 transition.
"""
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from jose import jwt as jose_jwt

from app.config import settings
from app.deps import current_tenant_id, require_operator, require_role, require_user
from app.models import Role, Tenant, User
from app.security import TokenError, create_operator_token, create_user_token, decode_token, hash_password

# --- Token shape -------------------------------------------------------


def test_operator_token_has_no_tenant_or_role_claim():
    token = create_operator_token(uuid.uuid4())
    payload = decode_token(token)
    assert payload["plane"] == "operator"
    assert "tenant_id" not in payload
    assert "role" not in payload


def test_user_token_carries_tenant_and_role():
    tenant_id = uuid.uuid4()
    token = create_user_token(uuid.uuid4(), tenant_id, Role.DEVELOPER.value)
    payload = decode_token(token)
    assert payload["plane"] == "user"
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["role"] == Role.DEVELOPER.value


async def _active_tenant(db_session, is_active: bool = True) -> uuid.UUID:
    tenant_id = uuid.uuid4()
    db_session.add(Tenant(id=tenant_id, name=f"jwt-plane-test-{uuid.uuid4().hex[:6]}", is_active=is_active))
    await db_session.flush()
    return tenant_id


async def _user(db_session, tenant_id: uuid.UUID, role: int, is_active: bool = True) -> uuid.UUID:
    """Committed User row backing a token's `sub` claim -- require_user now
    looks this row up (Phase 3.5), so a token minted for a uuid with no
    matching row is indistinguishable from a deactivated/deleted user."""
    user_id = uuid.uuid4()
    db_session.add(User(
        id=user_id, tenant_id=tenant_id, username=f"u-{user_id.hex[:8]}",
        email=f"{user_id.hex[:8]}@jwt-plane-test.local",
        hashed_password=hash_password("irrelevant-for-this-test"),
        role=role, is_active=is_active,
    ))
    await db_session.flush()
    return user_id


# --- Plane separation at the dependency layer ---------------------------


async def test_require_operator_rejects_a_user_token(db_session):
    """This is the actual mechanism that keeps operators structurally out
    of tenant-scoped routes -- and, symmetrically, keeps a user token from
    being accepted on an operator-only route."""
    token = create_user_token(uuid.uuid4(), uuid.uuid4(), Role.ADMIN.value)
    with pytest.raises(HTTPException) as exc:
        await require_operator(token, db_session)
    assert exc.value.status_code == 401


async def test_require_user_rejects_an_operator_token(db_session):
    token = create_operator_token(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await require_user(token, db_session)
    assert exc.value.status_code == 401


async def test_require_operator_rejects_missing_token(db_session):
    with pytest.raises(HTTPException) as exc:
        await require_operator(None, db_session)
    assert exc.value.status_code == 401


async def test_require_role_enforces_admin_only(db_session):
    tenant_id = await _active_tenant(db_session)
    admin_id = await _user(db_session, tenant_id, Role.ADMIN.value)
    trainee_id = await _user(db_session, tenant_id, Role.TRAINEE.value)
    admin_payload = await require_user(create_user_token(admin_id, tenant_id, Role.ADMIN.value), db_session)
    trainee_payload = await require_user(create_user_token(trainee_id, tenant_id, Role.TRAINEE.value), db_session)

    admin_only = require_role(Role.ADMIN.value)
    assert admin_only(admin_payload)["role"] == Role.ADMIN.value
    with pytest.raises(HTTPException):
        admin_only(trainee_payload)


async def test_require_role_allows_multiple_roles(db_session):
    tenant_id = await _active_tenant(db_session)
    dev_id = await _user(db_session, tenant_id, Role.DEVELOPER.value)
    trainee_id = await _user(db_session, tenant_id, Role.TRAINEE.value)
    dev_only_or_above = require_role(Role.ADMIN.value, Role.DEVELOPER.value)
    dev_payload = await require_user(create_user_token(dev_id, tenant_id, Role.DEVELOPER.value), db_session)
    trainee_payload = await require_user(create_user_token(trainee_id, tenant_id, Role.TRAINEE.value), db_session)

    assert dev_only_or_above(dev_payload)["role"] == Role.DEVELOPER.value
    with pytest.raises(HTTPException):
        dev_only_or_above(trainee_payload)


async def test_current_tenant_id_matches_token(db_session):
    tenant_id = await _active_tenant(db_session)
    user_id = await _user(db_session, tenant_id, Role.TRAINEE.value)
    payload = await require_user(create_user_token(user_id, tenant_id, Role.TRAINEE.value), db_session)
    assert current_tenant_id(payload) == tenant_id


# --- Tenant deactivation enforcement (Phase 3) ---------------------------


async def test_require_user_rejects_token_for_deactivated_tenant(db_session):
    """The actual enforcement side of PATCH /operator/tenants/{id} with
    is_active=false -- a still-valid, still-correctly-shaped user token
    must be rejected once its tenant is deactivated. Checked against the
    DB on every call, not baked into the JWT, so this takes effect
    immediately rather than waiting for the token to expire."""
    tenant_id = await _active_tenant(db_session, is_active=False)
    token = create_user_token(uuid.uuid4(), tenant_id, Role.ADMIN.value)
    with pytest.raises(HTTPException) as exc:
        await require_user(token, db_session)
    assert exc.value.status_code == 401
    # Safe per the error-message contract: no tenant id in the message.
    assert str(tenant_id) not in exc.value.detail["detail"]


async def test_require_user_rejects_token_for_nonexistent_tenant(db_session):
    """Belt-and-suspenders: a token whose tenant_id doesn't resolve to any
    row at all (deleted tenant, or a forged claim) fails the same way a
    deactivated one does, not with a different error that would reveal
    "this tenant id never existed" vs "it existed but was deactivated"."""
    token = create_user_token(uuid.uuid4(), uuid.uuid4(), Role.ADMIN.value)
    with pytest.raises(HTTPException) as exc:
        await require_user(token, db_session)
    assert exc.value.status_code == 401


async def test_require_user_accepts_token_for_active_tenant(db_session):
    tenant_id = await _active_tenant(db_session, is_active=True)
    user_id = await _user(db_session, tenant_id, Role.DEVELOPER.value)
    token = create_user_token(user_id, tenant_id, Role.DEVELOPER.value)
    payload = await require_user(token, db_session)
    assert payload["tenant_id"] == str(tenant_id)


async def test_require_user_rejects_token_for_deactivated_user(db_session):
    """Phase 3.5: same enforcement as tenant deactivation, one level down.
    A still-valid, still-correctly-shaped token for a user whose own
    is_active has been flipped to false (DELETE /admin/users) must be
    rejected immediately, not just once the token expires."""
    tenant_id = await _active_tenant(db_session, is_active=True)
    user_id = await _user(db_session, tenant_id, Role.DEVELOPER.value, is_active=False)
    token = create_user_token(user_id, tenant_id, Role.DEVELOPER.value)
    with pytest.raises(HTTPException) as exc:
        await require_user(token, db_session)
    assert exc.value.status_code == 401
    assert str(user_id) not in exc.value.detail["detail"]


async def test_require_user_rejects_token_for_nonexistent_user(db_session):
    """A token whose sub doesn't resolve to any user row (forged claim, or
    a hard-deleted row that shouldn't exist per the soft-delete design)
    fails the same way a deactivated one does."""
    tenant_id = await _active_tenant(db_session, is_active=True)
    token = create_user_token(uuid.uuid4(), tenant_id, Role.DEVELOPER.value)
    with pytest.raises(HTTPException) as exc:
        await require_user(token, db_session)
    assert exc.value.status_code == 401


# --- Attack scenarios a security-first project should defend against ----


def test_forged_token_with_wrong_secret_is_rejected():
    """An attacker who doesn't know JWT_SECRET_KEY cannot mint a valid
    token of either plane, even if they know the exact claim shape."""
    forged = jose_jwt.encode(
        {"sub": str(uuid.uuid4()), "plane": "operator"},
        "attacker-does-not-know-the-real-secret",
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(forged)


def _b64url(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_alg_none_token_is_rejected():
    """Classic JWT vulnerability: a token using alg=none (no signature at
    all) must be rejected outright, not merely fail signature checking.
    Built by hand rather than via jose.encode(): jose itself refuses to
    *produce* an alg=none token (JWSError at encode time), which is a
    library-level safety net, not proof decode_token() would reject one
    forged by a less careful library or by hand -- so this constructs the
    raw header.payload. token the way an attacker actually would."""
    header = _b64url({"alg": "none", "typ": "JWT"})
    payload = _b64url({"sub": str(uuid.uuid4()), "plane": "operator"})
    forged = f"{header}.{payload}."  # empty signature segment, as alg=none implies

    with pytest.raises(TokenError):
        decode_token(forged)


def test_operator_token_with_injected_tenant_id_fails_closed():
    """Even a token that's validly signed but shaped wrong -- an
    'operator' token that somehow carries a tenant_id claim, which
    create_operator_token() never produces -- must be rejected rather than
    silently trusted. This is decode_token's explicit fail-closed check."""
    forged = jose_jwt.encode(
        {"sub": str(uuid.uuid4()), "plane": "operator", "tenant_id": str(uuid.uuid4())},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(forged)


def test_user_token_missing_required_claims_fails_closed():
    """A 'user' plane token missing tenant_id or role (e.g. hand-crafted,
    or from a future bug that omits a claim) must be rejected, not treated
    as a valid-but-scopeless user."""
    forged = jose_jwt.encode(
        {"sub": str(uuid.uuid4()), "plane": "user"},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(forged)


def test_unknown_plane_value_fails_closed():
    forged = jose_jwt.encode(
        {"sub": str(uuid.uuid4()), "plane": "superadmin"},
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(forged)


def test_expired_token_is_rejected():
    """Found missing during a Phase-2 audit (Session 6/7): every attack
    scenario in this file above tests a malformed or forged token, but none
    tested the single most common real-world case -- a token past its own
    exp. Hand-crafted here (not via create_user_token, which always sets a
    future expiry) with a timestamp already in the past, the way an old,
    legitimately-issued token actually looks once JWT_EXPIRE_MINUTES has
    elapsed."""
    expired = jose_jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "plane": "user",
            "tenant_id": str(uuid.uuid4()),
            "role": Role.ADMIN.value,
            "jti": str(uuid.uuid4()),
            "exp": datetime.now(timezone.utc) - timedelta(minutes=5),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(TokenError):
        decode_token(expired)
