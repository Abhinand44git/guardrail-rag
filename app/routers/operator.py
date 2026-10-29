"""
Operator-plane endpoints (Phase 3).

Everything here requires an operator-plane JWT (Depends(require_operator)) --
operators have no tenant_id claim at all, so they structurally cannot reach
any tenant-scoped route, and no tenant-scoped route can reach these either.
See app/deps.py and the decision log's "Roles and identity" section.

Route prefix is /operator, not /auth/operator -- see the naming-drift note
in app/routers/auth.py. The old /auth/operator/login path is gone, not
aliased.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import blacklist_token, require_operator, require_operator_plane_logout
from app.email import EmailSendError, send_new_user_welcome_email
from app.models import Operator, Role, Tenant, User
from app.schemas import (
    AdminCreateRequest,
    AdminCreateResponse,
    LoginRequest,
    TenantCreateRequest,
    TenantPatchRequest,
    TenantResponse,
    TokenResponse,
)
from app.security import create_operator_token, generate_temp_password, hash_password, verify_password
from app.user_provisioning import check_admin_cap_or_raise, check_username_available_or_raise

router = APIRouter(prefix="/operator", tags=["operator"])

_BAD_CREDENTIALS = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail={"error": "unauthorized", "detail": "invalid email or password"},
)


def _not_found(what: str) -> HTTPException:
    # Generic per the error-message contract -- names the resource *type*
    # only, never the id/tenant that was searched for.
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "detail": f"{what} not found"},
    )


async def _get_tenant_or_404(db: AsyncSession, tenant_id: uuid.UUID) -> Tenant:
    result = await db.execute(select(Tenant).where(Tenant.id == tenant_id))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise _not_found("tenant")
    return tenant


@router.post("/login", response_model=TokenResponse)
async def operator_login(body: LoginRequest, db: AsyncSession = Depends(get_db)):
    """Moved from /auth/operator/login -- see module docstring."""
    result = await db.execute(select(Operator).where(Operator.email == body.email))
    operator = result.scalar_one_or_none()
    if not operator or not verify_password(body.password, operator.hashed_password):
        raise _BAD_CREDENTIALS
    token = create_operator_token(operator.id)
    return TokenResponse(access_token=token, plane="operator")


@router.post("/tenants", response_model=TenantResponse, status_code=status.HTTP_201_CREATED)
async def create_tenant(
    body: TenantCreateRequest,
    payload: dict = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """
    Creates a tenant with NO AI model configured (llm_provider/llm_model_name/
    llm_api_key_ref all stay NULL -- that's the column default now, see
    migration 0004). /query remains unusable for this tenant until an Admin
    runs PATCH /admin/tenant-config. This is expected, not an error state.
    """
    tenant = Tenant(
        id=uuid.uuid4(),
        name=body.name,
        max_admins=body.max_admins,
        is_active=True,
        created_by_operator_id=uuid.UUID(payload["sub"]),
    )
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.patch("/tenants/{tenant_id}", response_model=TenantResponse)
async def patch_tenant(
    tenant_id: uuid.UUID,
    body: TenantPatchRequest,
    payload: dict = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """
    Tenant lifecycle only: is_active and/or max_admins. Does NOT touch AI
    model config -- that's PATCH /admin/tenant-config, Admin-owned, on
    purpose (decision log item #5).

    Lowering max_admins is a "grandfather" change: it does not remove or
    demote any existing Admin. The DB trigger (trg_enforce_max_admins) only
    fires on INSERT, so existing rows are untouched -- the new cap just
    blocks *new* Admin creation until the count naturally drops back under
    it. See tests/test_operator_endpoints.py for the behavioral proof.
    """
    tenant = await _get_tenant_or_404(db, tenant_id)

    if body.is_active is not None:
        tenant.is_active = body.is_active
    if body.max_admins is not None:
        tenant.max_admins = body.max_admins

    await db.commit()
    await db.refresh(tenant)
    return tenant


@router.post(
    "/tenants/{tenant_id}/admin",
    response_model=AdminCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_tenant_admin(
    tenant_id: uuid.UUID,
    body: AdminCreateRequest,
    payload: dict = Depends(require_operator),
    db: AsyncSession = Depends(get_db),
):
    """
    Creates the tenant's FIRST Admin only, with a random, emailed password
    (decision log item #4). Locked as first-admin-only this session: any
    Admin beyond the first must be created by an existing Admin (Phase 3.5's
    POST /admin/users), not by the Operator calling this endpoint again.

    This does mean a tenant with max_admins > 1 has no way to get a second
    Admin until Phase 3.5 ships -- a known, accepted temporary gap, not an
    oversight (see decision log).

    All-or-nothing: the DB write and the email send succeed together or the
    whole thing rolls back. An Admin account that exists but whose owner has
    no way to learn the password is a support burden, not a partial success.
    """
    tenant = await _get_tenant_or_404(db, tenant_id)

    # Both pre-checks now live in app/user_provisioning.py, shared with the
    # future POST /admin/users (Phase 3.5) -- see that module's docstring
    # for why the admin-cap check is parameterized (max_allowed=1 here,
    # tenant.max_admins there) while the username check is identical either
    # way. The DB trigger from migration 0002 still fires as a race-safety
    # net below.
    await check_admin_cap_or_raise(db, tenant.id, max_allowed=1)
    await check_username_available_or_raise(db, tenant.id, body.username)

    temp_password = generate_temp_password()
    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        username=body.username,
        email=body.email,
        hashed_password=hash_password(temp_password),
        role=Role.ADMIN.value,
    )
    db.add(user)

    try:
        await db.flush()  # let the DB-level constraints/trigger run as a race-safety net
    except DBAPIError:
        # Both pre-checks already passed, so this only fires on a genuine
        # race (another request landed between the pre-check and this
        # flush) -- could be another Admin having just been created for
        # this tenant, or a username collision. A generic-but-safe message
        # is more honest than guessing which one it was.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_input",
                "detail": "This Admin could not be created — please check whether this tenant already has an Admin and whether the username is available, then try again.",
            },
        )

    try:
        # Phase 3.5 generalized this to send_new_user_welcome_email(role_name=...)
        # so POST /admin/users can reuse the identical send-or-rollback mechanism
        # for Developer/Trainee/Admin, not just Admin -- see app/email.py.
        send_new_user_welcome_email(user.email, temp_password, role_name="Admin")
    except EmailSendError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "server_error",
                "detail": "Admin account could not be created — the welcome email failed to send.",
            },
        )

    await db.commit()
    await db.refresh(user)
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def operator_logout(
    payload: dict = Depends(require_operator_plane_logout),
    db: AsyncSession = Depends(get_db),
):
    """
    Operator-plane counterpart to POST /auth/logout, added in Session 9
    when logout was split by plane (previously one endpoint accepted
    either token -- see app/routers/auth.py::logout's docstring for why
    that changed). A user-plane token presented here is rejected (401) by
    require_operator_plane_logout before this function body runs.

    Same idempotent-by-design behavior as the user-plane route: logging out
    an already-blacklisted token, or calling this twice, returns the same
    204, not an error.
    """
    await blacklist_token(db, payload)
