import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import blacklist_token, require_user_password_change_allowed, require_user_plane_logout
from app.errors import db_failed
from app.models import User
from app.password_policy import check_new_password
from app.schemas import ChangePasswordRequest, LoginRequest, TokenResponse
from app.security import create_user_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# Same generic error for both planes and both failure modes (no such account /
# wrong password) — don't let the response shape reveal which one it was.
_BAD_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"error": "unauthorized", "detail": "invalid email or password"},
)

# Operator login lives at POST /operator/login (app/routers/operator.py), not
# here. Shipped code originally had it at /auth/operator/login; the Design
# doc corrected this to /operator/login so all operator-plane routes share
# one prefix, and that's the name we're keeping going forward — the old
# /auth/operator/login path is removed, not aliased. See decision log.

# Phase 3.5 addition: distinct from _BAD_CREDENTIALS on purpose, same
# reasoning as app/deps.py's _USER_DEACTIVATED_ERROR -- reaching this point
# means the password was correct, so the account's existence and the
# credential's validity are already established. Naming "deactivated" here
# isn't a new leak beyond that; giving the generic "invalid email or
# password" instead would just make a legitimately-locked-out user think
# they mistyped their own password.
_ACCOUNT_DEACTIVATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={
        "error": "unauthorized",
        "detail": "this account has been deactivated — contact your Admin",
    },
)


# ADR-0026. Reached only after the password has verified, so (like the deactivated message) naming
# the cause reveals nothing to someone who does not already hold the credential.
_TEMP_PASSWORD_EXPIRED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={
        "error": "unauthorized",
        "detail": "your temporary password has expired — ask an Admin to issue a new one",
    },
)

_WRONG_CURRENT_PASSWORD = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail={"error": "invalid_input", "detail": "the current password is incorrect"},
)


@router.post("/login", response_model=TokenResponse)
async def user_login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.email == body.email))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.password, user.hashed_password):
        raise _BAD_CREDENTIALS
    # Phase 3.5: a deactivated user (DELETE /admin/users) must not be able
    # to mint a fresh token even though their password still verifies --
    # otherwise deactivation would only stop someone already holding a
    # live token (see app/deps.py::require_user), not someone logging in
    # after the fact.
    if not user.is_active:
        raise _ACCOUNT_DEACTIVATED
    # ADR-0026: an unused temporary password stops working after its expiry. Only while it is still
    # temporary: once the person has set a real password there is nothing to expire.
    if (
        user.must_change_password
        and user.temp_password_expires_at is not None
        and user.temp_password_expires_at < datetime.now(timezone.utc)
    ):
        raise _TEMP_PASSWORD_EXPIRED
    token = create_user_token(user.id, user.tenant_id, user.role, user.session_version)
    return TokenResponse(access_token=token, plane="user", password_change_required=user.must_change_password)


@router.post("/change-password", response_model=TokenResponse)
async def change_password(
    body: ChangePasswordRequest,
    payload: dict = Depends(require_user_password_change_allowed),
    db: AsyncSession = Depends(get_db),
):
    """
    Set a new password (ADR-0026). Open to a user who still holds a temporary password (that is who
    it is for -- it is the one door, with logout, that stays open to them) and to any user who just
    wants to change theirs.

    Needs the CURRENT password even though the caller is logged in: a stolen or left-open session
    must not be enough to take over the account. On success every older session of this user is
    invalidated (session_version is bumped) and a fresh token is returned, so the caller stays
    logged in and everyone else is signed out. No rate limit yet (Phase 6): a stolen token could
    guess the current password, which is a named gap, not a forgotten one.
    """
    user = await db.get(User, uuid.UUID(payload["sub"]))  # already loaded by the dependency
    if not verify_password(body.current_password, user.hashed_password):
        raise _WRONG_CURRENT_PASSWORD
    problems = check_new_password(
        body.new_password, current=body.current_password, username=user.username, email=user.email
    )
    if problems:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_input", "detail": "the new password " + "; it ".join(problems)},
        )

    user.hashed_password = hash_password(body.new_password)
    user.must_change_password = False
    user.temp_password_expires_at = None
    user.session_version = User.session_version + 1  # atomic in SQL: two racing changes cannot lose a bump
    try:
        await db.commit()
    except DBAPIError as e:
        await db.rollback()
        raise db_failed() from e
    await db.refresh(user)
    token = create_user_token(user.id, user.tenant_id, user.role, user.session_version)
    return TokenResponse(access_token=token, plane="user", password_change_required=False)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: dict = Depends(require_user_plane_logout),
    db: AsyncSession = Depends(get_db),
):
    """
    The actual fix for the "token blacklist" item in Phase 2's own
    build-phase description -- never built until Session 7. Blacklists the
    presented token's jti, so app/deps.py's require_user rejects it on
    every subsequent request for the remainder of its natural lifetime,
    instead of it quietly working until JWT_EXPIRE_MINUTES runs out.

    User-plane only (Session 9) -- Session 7 originally made this accept
    either plane's token, flagged at the time as an interpretation beyond
    the Analysis doc's literal "Auth (tenant users)" scoping. Split here
    into this route plus the new POST /operator/logout, so logout now
    mirrors every other route's plane separation instead of being the one
    exception: an operator token presented here is rejected (401) by
    require_user_plane_logout before this function body ever runs, the
    same way require_user would reject one.

    Idempotent by design: logging out an already-blacklisted token (or
    calling this twice with the same token) returns the same 204, not an
    error -- see require_user_plane_logout's docstring for why the
    dependency itself doesn't already reject a blacklisted token.
    """
    await blacklist_token(db, payload)
