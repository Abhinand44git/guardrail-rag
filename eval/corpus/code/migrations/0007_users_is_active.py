"""users: add is_active (soft deactivation)

Phase 3.5 decision (Analysis doc §2/§6, decision log): DELETE /admin/users
is a soft deactivation, not a hard row delete. project_memory.created_by_user_id
and query_log.user_id both need to keep resolving to a real user row -- a hard
delete would either break that attribution or force cascading deletes that
destroy the exact SDLC history the project-memory feature exists to keep.

Mirrors tenants.is_active (migration 0005) -- same precedent, not a new
mechanism. Defaults to true: every existing/newly-created user is active
until an Admin explicitly deactivates them.

This column is also what migration 0008's trigger update depends on --
counting only is_active = true Admins toward tenants.max_admins requires
this column to exist first.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-12

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("users", "is_active")
