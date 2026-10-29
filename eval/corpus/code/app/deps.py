import uuid
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Tenant, TokenBlacklist, User
from app.security import TokenError, decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# Generic error message — per decision log, never leak tenant IDs/role numbers/schema
# details to the client. Real detail goes server-side against a request ID (added
# when logging middleware is built in Phase 6).
_AUTH_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"error": "unauthorized", "detail": "invalid or expired credentials"},
)

# Distinct from _AUTH_ERROR on purpose: the token itself is valid here (it proves
# the holder really was a legitimate user of this tenant), so telling them their
# org's access was deactivated isn't leaking anything they don't already know --
# it's the actionable version of the same denial. Still names no tenant ID, role
# number, or schema detail, so it stays inside the error-message contract.
_TENANT_DEACTIVATED_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={
        "error": "unauthorized",
        "detail": "this organization's access has been deactivated — contact your Operator",
    },
)

# Phase 3.5 addition (not present in Phase 3): same reasoning as
# _TENANT_DEACTIVATED_ERROR above, one level down. A still-valid token proves
# the holder really was a legitimate user, so naming the actionable cause
# isn't leaking anything an attacker holding that token doesn't already
# know. Distinct wording from the tenant case so a user isn't told to
# contact the Operator for something only their own tenant's Admin can fix.
_USER_DEACTIVATED_ERROR = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={
        "error": "unauthorized",
        "detail": "this account has been deactivated — contact your Admin",
    },
)


def _decode_or_401(token: str | None) -> dict:
    if not token:
        raise _AUTH_ERROR
    try:
        return decode_token(token)
    except TokenError:
        raise _AUTH_ERROR


async def _is_blacklisted(db: AsyncSession, jti: str | None) -> bool:
    if not jti:
        # Tokens minted before migration 0012 (every token minted by any
        # pre-this-fix deployment) carry no jti at all -- there's nothing
        # to look up, so they're treated as "not blacklisted" rather than
        # erroring. They simply can't be individually revoked; they still
        # expire normally. Backward-compatible by construction, not by
        # special-casing an old token format.
        return False
    return await db.get(TokenBlacklist, jti) is not None


def require_user_plane_logout(token: str = Depends(oauth2_scheme)) -> dict:
    """
    Decode-only gate for POST /auth/logout specifically (plane-restricted
    to "user", unlike the removed require_any_plane this replaces -- see
    decision log, Session 9: logout now mirrors every other route's plane
    separation instead of being the one place that accepted either).
    Deliberately does NOT check the blacklist, unlike require_user above:
    logging out an already-blacklisted token should be a harmless no-op
    (idempotent 204), not a confusing 401 on a second call. Still requires
    a structurally valid, unexpired, correctly-shaped user-plane token --
    the same decode_token() every other dependency uses -- so this can't be
    used to blacklist an arbitrary jti an attacker doesn't actually hold a
    valid token for, and an operator token is rejected here just as it
    would be by require_user.
    """
    payload = _decode_or_401(token)
    if payload["plane"] != "user":
        raise _AUTH_ERROR
    return payload


def require_operator_plane_logout(token: str = Depends(oauth2_scheme)) -> dict:
    """Operator-plane counterpart to require_user_plane_logout, for the new
    POST /operator/logout. Same reasoning, mirrored."""
    payload = _decode_or_401(token)
    if payload["plane"] != "operator":
        raise _AUTH_ERROR
    return payload


async def blacklist_token(db: AsyncSession, payload: dict) -> None:
    """
    Shared by POST /auth/logout and POST /operator/logout so both insert
    into token_blacklist identically instead of duplicating the logic.
    Idempotent: a token already blacklisted (or with no jti at all -- see
    _is_blacklisted above) is a silent no-op, not an error.
    """
    jti = payload.get("jti")
    if not jti:
        # Token minted before migration 0012 -- nothing to blacklist by.
        # It keeps working until it naturally expires; not an error case.
        return

    if await db.get(TokenBlacklist, jti) is not None:
        return  # already logged out

    tenant_id = payload.get("tenant_id")  # absent for an operator-plane token
    db.add(TokenBlacklist(
        token_jti=jti,
        tenant_id=uuid.UUID(tenant_id) if tenant_id else None,
        expires_at=datetime.fromtimestamp(payload["exp"], tz=timezone.utc),
    ))
    await db.commit()


async def require_operator(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> dict:
    payload = _decode_or_401(token)
    if payload["plane"] != "operator":
        # Operator JWTs never carry tenant_id, so a user-plane token lands here
        # and is rejected on the plane check alone — before any route logic runs.
        raise _AUTH_ERROR

    # Same reasoning as require_user's blacklist check below -- added here
    # too since POST /auth/logout accepts either plane's token (see
    # app/routers/auth.py), so an operator's logged-out token needs to
    # actually stop working, not just a user's.
    if await _is_blacklisted(db, payload.get("jti")):
        raise _AUTH_ERROR

    return payload


async def require_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> dict:
    payload = _decode_or_401(token)
    if payload["plane"] != "user":
        raise _AUTH_ERROR

    # Checked against the DB on every request, same reasoning as the
    # tenant/user is_active checks just below: baking revocation into the
    # JWT itself would mean a logged-out token keeps working until it
    # naturally expires, which defeats the point of POST /auth/logout
    # existing at all. This was the actual "token blacklist" gap -- see
    # decision log, Session 7.
    if await _is_blacklisted(db, payload.get("jti")):
        raise _AUTH_ERROR

    # Checked against the DB on every request, not encoded as a JWT claim --
    # a claim would mean a deactivated tenant's users stay able to act until
    # their token naturally expires or refreshes, which defeats the point of
    # an Operator being able to cut off access immediately (PATCH
    # /operator/tenants/{id} with is_active=false).
    tenant = await db.get(Tenant, uuid.UUID(payload["tenant_id"]))
    if tenant is None or not tenant.is_active:
        raise _TENANT_DEACTIVATED_ERROR

    # Phase 3.5 addition: same check, one level down, now that users.is_active
    # exists and DELETE /admin/users can deactivate an individual user.
    # Without this, a user deactivated mid-session would keep working with
    # their existing token for up to JWT_EXPIRE_MINUTES -- the exact gap
    # Session 3 closed for tenants, now closed for users for the same reason.
    user = await db.get(User, uuid.UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise _USER_DEACTIVATED_ERROR

    return payload


def require_role(*allowed_roles: int):
    """Usage: Depends(require_role(Role.ADMIN.value))"""
    def _dep(payload: dict = Depends(require_user)) -> dict:
        if payload["role"] not in allowed_roles:
            raise _AUTH_ERROR
        return payload
    return _dep


def current_tenant_id(payload: dict = Depends(require_user)) -> uuid.UUID:
    return uuid.UUID(payload["tenant_id"])
