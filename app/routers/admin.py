"""
Admin-plane endpoints (Phase 3.5): tenant user management.

Every route here is Admin-only (Depends(require_role(Role.ADMIN.value))) and
implicitly tenant-scoped via the caller's own JWT (current_tenant_id) --
there is no tenant_id path/body parameter anywhere below for a caller to
spoof. An Admin can only ever act on users inside their own tenant.

Reuses two things Phase 3 built specifically so this phase wouldn't
duplicate them:
  - app/user_provisioning.py's check_admin_cap_or_raise /
    check_username_available_or_raise (see that module's docstring for why
    the cap check takes max_allowed as an argument).
  - app/email.py's send_new_user_welcome_email, generalized this phase from
    Admin-only wording to accept any role name, since this endpoint (unlike
    the Operator's) can create all three roles.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import current_tenant_id, require_role
from app.email import EmailSendError, send_new_user_welcome_email
from app.models import Role, Tenant, User
from app.schemas import (
    UserCreateRequest,
    UserListResponse,
    UserPatchRequest,
    UserResponse,
)
from app.security import generate_temp_password, hash_password
from app.user_provisioning import check_admin_cap_or_raise, check_username_available_or_raise

router = APIRouter(prefix="/admin", tags=["admin"])

_ROLE_NAMES = {
    Role.ADMIN.value: "Admin",
    Role.DEVELOPER.value: "Developer",
    Role.TRAINEE.value: "Trainee",
}

# Not an explicit decision in the source docs -- see patch_user()'s
# docstring for the reasoning. Flagged here as a judgment call, not a
# silently-applied assumption.
_SELF_TARGET_ERROR = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail={
        "error": "invalid_input",
        "detail": (
            "You cannot change your own role or active status through this "
            "endpoint. Ask another Admin, or use the self-service "
            "password-change flow for your own credentials."
        ),
    },
)

# Reactivation-specific (is_active: false -> true). Not a security boundary
# -- the acting Admin already sees every user's email via GET /admin/users
# -- just a deliberate second look before restoring access, aimed at the
# "clicked the wrong row in a list" mistake. Checked before the admin-cap
# check below: if it's the wrong person, whether there's cap room for them
# is irrelevant.
_REACTIVATION_CONFIRM_ERROR = HTTPException(
    status_code=status.HTTP_400_BAD_REQUEST,
    detail={
        "error": "invalid_input",
        "detail": (
            "To reactivate this user, provide confirm_email matching their "
            "registered email exactly."
        ),
    },
)


def _user_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "detail": "user not found"},
    )


async def _get_user_in_tenant_or_404(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID) -> User:
    """
    Tenant-scoped lookup, not a bare db.get(User, user_id) -- an Admin in
    tenant A must get the exact same generic 404 for a user id that doesn't
    exist at all as for one that exists but belongs to tenant B. Anything
    else would let an Admin fingerprint other tenants' user ids by the
    error shape alone, which the error-message contract (never leak
    cross-tenant structure) exists specifically to prevent.
    """
    result = await db.execute(select(User).where(User.id == user_id, User.tenant_id == tenant_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _user_not_found()
    return user


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    payload: dict = Depends(require_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """
    Creates a Developer, Trainee, or Admin in the caller's own tenant.
    Identical mechanism to POST /operator/tenants/{id}/admin: a random
    password is generated and emailed, never returned in this response.
    Creating as Admin goes through the same admin-cap pre-check as that
    endpoint, parameterized by this tenant's own max_admins instead of the
    Operator endpoint's fixed 1 (see app/user_provisioning.py).
    """
    tenant_id = current_tenant_id(payload)
    # require_role -> require_user already proved this tenant exists and is
    # active before this route ever runs; a plain get is safe here.
    tenant = await db.get(Tenant, tenant_id)

    if body.role == Role.ADMIN.value:
        await check_admin_cap_or_raise(db, tenant_id, max_allowed=tenant.max_admins)
    await check_username_available_or_raise(db, tenant_id, body.username)

    temp_password = generate_temp_password()
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        username=body.username,
        email=body.email,
        hashed_password=hash_password(temp_password),
        role=body.role,
    )
    db.add(user)

    try:
        await db.flush()  # DB trigger (migration 0008) is the race-safety net under the pre-check above
    except DBAPIError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_input",
                "detail": (
                    "This user could not be created — please check whether "
                    "the admin limit has been reached and whether the "
                    "username is available, then try again."
                ),
            },
        )

    try:
        send_new_user_welcome_email(user.email, temp_password, role_name=_ROLE_NAMES[body.role])
    except EmailSendError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "server_error",
                "detail": "User account could not be created — the welcome email failed to send.",
            },
        )

    await db.commit()
    await db.refresh(user)
    return user


@router.get("/users", response_model=UserListResponse)
async def list_users(
    role: int | None = None,
    limit: int = 50,
    offset: int = 0,
    payload: dict = Depends(require_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """
    Paginated, tenant-scoped, optionally filtered by role (Analysis §2:
    "pagination and role filtering required"). Ordered by created_at then
    id -- the spec says ordering isn't *required*, but pagination with no
    ORDER BY at all is a real correctness bug (Postgres doesn't guarantee
    stable row order across two LIMIT/OFFSET calls without one, so pages
    could silently skip or repeat rows); this is the smallest deterministic
    default, not the "ordering feature" the spec left optional.
    """
    tenant_id = current_tenant_id(payload)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    filters = [User.tenant_id == tenant_id]
    if role is not None:
        filters.append(User.role == role)

    total = (await db.execute(select(func.count()).select_from(User).where(*filters))).scalar_one()
    result = await db.execute(
        select(User).where(*filters).order_by(User.created_at, User.id).limit(limit).offset(offset)
    )
    users = result.scalars().all()

    return UserListResponse(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.patch("/users/{user_id}", response_model=UserResponse)
async def patch_user(
    user_id: uuid.UUID,
    body: UserPatchRequest,
    payload: dict = Depends(require_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """
    Partial update: role and/or is_active, either or both in one request --
    same pattern as PATCH /operator/tenants/{id}. Promoting to Admin, or
    reactivating a previously-deactivated Admin, runs the same admin-cap
    pre-check as POST; migration 0008's trigger update enforces the
    identical rule at the DB layer underneath, covering the exact race
    window this pre-check can't close on its own.

    Self-targeting guard: an Admin cannot use this endpoint on their own
    user id (see _SELF_TARGET_ERROR). This is a judgment call, not a
    decision spelled out in the source docs -- the Analysis doc
    consistently describes this endpoint in terms of one Admin acting on
    "another"/"other" user, and self-service password change is already
    carved out as its own separate, differently-scoped endpoint. Concretely:
    without this guard, a tenant's sole Admin could deactivate or demote
    themselves and lock the tenant out of Admin access entirely, since
    Operators have no path back into a tenant's users at all (structural,
    by design). Worth revisiting if a real workflow needs it.

    Reactivation confirmation: any request that flips is_active from false
    to true (i.e. restores a deactivated user's access) must also include
    confirm_email matching that user's stored email exactly. This is not a
    security boundary -- the acting Admin already has full visibility into
    every user's email via GET /admin/users -- it's a deliberate speed bump
    against the specific mistake of reactivating the wrong row out of a
    list of deactivated users. Only reactivation triggers it: deactivating,
    promoting/demoting an already-active user, and re-sending an unchanged
    is_active value are all unaffected.
    """
    tenant_id = current_tenant_id(payload)
    if str(user_id) == payload["sub"]:
        raise _SELF_TARGET_ERROR

    tenant = await db.get(Tenant, tenant_id)
    user = await _get_user_in_tenant_or_404(db, tenant_id, user_id)

    is_reactivation = body.is_active is True and user.is_active is False
    if is_reactivation:
        if not body.confirm_email or body.confirm_email.strip() != user.email:
            raise _REACTIVATION_CONFIRM_ERROR

    was_active_admin = user.role == Role.ADMIN.value and user.is_active
    new_role = body.role if body.role is not None else user.role
    new_is_active = body.is_active if body.is_active is not None else user.is_active
    becomes_active_admin = new_role == Role.ADMIN.value and new_is_active

    if becomes_active_admin and not was_active_admin:
        await check_admin_cap_or_raise(db, tenant_id, max_allowed=tenant.max_admins)

    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active

    try:
        await db.flush()
    except DBAPIError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_input",
                "detail": "This update could not be applied — please check the admin limit and try again.",
            },
        )

    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", response_model=UserResponse)
async def deactivate_user(
    user_id: uuid.UUID,
    payload: dict = Depends(require_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """
    Soft deactivation, not a row delete (decision log): project_memory and
    query_log both need created_by_user_id/user_id to keep resolving to a
    real row, which is the entire point of the SDLC-memory feature. DELETE
    is kept as the HTTP verb for semantic clarity -- the caller's intent is
    "remove this person" -- even though it's implemented as
    is_active = false underneath.

    No admin-cap check needed here: deactivation only ever *reduces* the
    active-admin count, so it can't push a tenant over its cap. (Freeing a
    slot is the actual point of allowing it -- see migration 0008.)

    Same self-targeting guard as PATCH, same reasoning -- see that
    endpoint's docstring.
    """
    tenant_id = current_tenant_id(payload)
    if str(user_id) == payload["sub"]:
        raise _SELF_TARGET_ERROR

    user = await _get_user_in_tenant_or_404(db, tenant_id, user_id)
    user.is_active = False

    await db.commit()
    await db.refresh(user)
    return user
