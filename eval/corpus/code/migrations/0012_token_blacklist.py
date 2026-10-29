"""token_blacklist: create table

Phase 2's own build-phase description ("Security core: ...JWT plane
separation, token blacklist") promised this table; it was never built.
Confirmed by unzipping and re-running the *original* Phase 2 deliverable
directly, not just grepping the current codebase: no `token_blacklist`
table in any migration from `0001` onward, no `/auth/logout` route, and no
`jti` claim minted by any token at all -- so there was nothing for a
blacklist to even reference until this migration's companion change in
`app/security.py::_encode()`.

Primary key is `token_jti` alone, matching the Design doc's schema exactly
-- a jti is already globally unique (uuid4, minted fresh per token), so no
composite key with tenant_id is needed. `tenant_id` stays nullable, also
matching the Design doc: operator tokens carry no tenant_id claim, so a
blacklisted operator token's row has nothing to put there.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-29

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_blacklist",
        sa.Column("token_jti", sa.String(36), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True),
        sa.Column("blacklisted_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Supports a future cleanup job (not built yet) pruning rows whose
    # underlying token has already naturally expired, without a full table
    # scan.
    op.create_index("ix_token_blacklist_expires_at", "token_blacklist", ["expires_at"])


def downgrade() -> None:
    op.drop_table("token_blacklist")
