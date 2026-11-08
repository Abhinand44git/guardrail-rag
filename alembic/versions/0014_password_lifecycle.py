"""Batch C: password lifecycle columns (forced change, temporary-password expiry, session revocation)

users:      must_change_password BOOLEAN NOT NULL DEFAULT false
            temp_password_expires_at TIMESTAMPTZ NULL
            session_version INT NOT NULL DEFAULT 0
operators:  must_change_password BOOLEAN NOT NULL DEFAULT false
            session_version INT NOT NULL DEFAULT 0

Every existing row gets the defaults: nobody is forced to change a password and nobody's session is
revoked by this migration. There is no way to tell which existing passwords were temporary ones, so
a temporary password emailed BEFORE this migration stays valid until its owner changes it (ADR-0026).
No data is moved or rewritten. Downgrade drops the columns.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.text("false")))
    op.add_column("users", sa.Column("temp_password_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("session_version", sa.Integer, nullable=False, server_default=sa.text("0")))
    op.add_column("operators", sa.Column("must_change_password", sa.Boolean, nullable=False, server_default=sa.text("false")))
    op.add_column("operators", sa.Column("session_version", sa.Integer, nullable=False, server_default=sa.text("0")))


def downgrade() -> None:
    op.drop_column("operators", "session_version")
    op.drop_column("operators", "must_change_password")
    op.drop_column("users", "session_version")
    op.drop_column("users", "temp_password_expires_at")
    op.drop_column("users", "must_change_password")
