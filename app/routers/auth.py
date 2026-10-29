from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import blacklist_token, require_user_plane_logout
from app.models import User
from app.schemas import LoginRequest, TokenResponse
from app.security import create_user_token, verify_password

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
    token = create_user_token(user.id, user.tenant_id, user.role)
    return TokenResponse(access_token=token, plane="user")


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
