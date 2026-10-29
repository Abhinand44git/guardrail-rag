"""
Shared pre-checks for creating a user within a tenant.

Used today by POST /operator/tenants/{id}/admin (Phase 3, first-admin-only).
Written to be used by the future POST /admin/users (Phase 3.5) too, rather
than duplicated there -- see decision log item #6.

Note the admin-cap check is parameterized, not identical between the two
callers: the Operator's endpoint is locked to first-admin-only (always
passes max_allowed=1), while /admin/users will need to allow up to
tenant.max_admins. Same underlying rule ("reject if admin_count >=
max_allowed"), different cap per caller -- that's why this takes
max_allowed as an argument instead of hardcoding either value.

The username-uniqueness check has no such variation -- it's the exact same
rule for both callers, taken as-is.

Phase 3.5 addition, not in the original Phase 3 version of this file: the
admin count below now filters on is_active = true. This wasn't optional --
once users.is_active exists and DELETE /admin/users can deactivate an
Admin to free their max_admins slot (decision log), a pre-check that still
counted deactivated Admins would give a wrong "cap reached" rejection for
a request the DB trigger (migration 0008) would actually allow. Both layers
have to agree on what "an admin" means for the same reason they had to
agree on the cap number itself.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User


async def check_admin_cap_or_raise(
    db: AsyncSession, tenant_id: uuid.UUID, max_allowed: int
) -> None:
    """
    Raises a safe 400 if the tenant already has `max_allowed` (or more)
    Admins. Caller decides what max_allowed means for them:
      - Operator's first-admin-only endpoint always passes 1.
      - /admin/users (Phase 3.5) will pass tenant.max_admins.
    Message is worded differently for the two cases -- "already has an
    Admin" reads oddly when max_allowed is 3, and "reached its maximum"
    reads oddly when max_allowed is 1.
    """
    result = await db.execute(
        select(func.count()).select_from(User).where(
            User.tenant_id == tenant_id,
            User.role == Role.ADMIN.value,
            User.is_active.is_(True),
        )
    )
    admin_count = result.scalar_one()
    if admin_count >= max_allowed:
        detail = (
            "This tenant already has an Admin. Additional Admins must be created by an existing Admin."
            if max_allowed == 1 else
            "This tenant has reached its maximum number of Admins."
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_input", "detail": detail},
        )


async def check_username_available_or_raise(
    db: AsyncSession, tenant_id: uuid.UUID, username: str
) -> None:
    """Raises a safe 400 if `username` is already taken within this tenant.
    Matches the DB-level uq_users_tenant_username constraint (migration
    0006) -- this is the precise, safe-error version of that constraint,
    not a replacement for it."""
    result = await db.execute(
        select(User.id).where(User.tenant_id == tenant_id, User.username == username)
    )
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_input",
                "detail": "This username is already taken in this tenant.",
            },
        )
