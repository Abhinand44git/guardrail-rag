"""tenants: add is_active and created_by_operator_id

Both columns are specified in the Design doc's tenants schema but were never
migrated in during Phase 1/2 -- there was no operator-plane code yet that
needed them. Phase 3 is what actually uses them:

  - is_active: PATCH /operator/tenants/{id} activates/deactivates a tenant.
    Defaults to true -- a newly created tenant is active immediately (it
    just has no AI model configured yet, which is a separate, independent
    kind of "not usable" tracked by the llm_* columns, not by is_active).
  - created_by_operator_id: set once at tenant-creation time by
    POST /operator/tenants. Nullable (matches the Design doc's SQL, which
    does not mark it NOT NULL) -- kept optional rather than required so old
    or manually-inserted rows without a known creating operator don't need
    a synthetic backfill value.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-30

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "created_by_operator_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("operators.id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "created_by_operator_id")
    op.drop_column("tenants", "is_active")
