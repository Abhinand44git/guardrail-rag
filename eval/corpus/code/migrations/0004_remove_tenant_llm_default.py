"""tenants.llm_provider / llm_model_name: drop NOT NULL + Groq default

0001 shipped these two columns as NOT NULL with server_default 'groq' /
'llama-3.1-8b-instant' -- a leftover from the pre-v2.0 architecture where
GuardRail shipped its own default generation model. That idea was corrected
and fully reversed (see decision log, "Major correction" section): GuardRail
owns no generation model of any kind. A tenant must have NULL here until an
Admin explicitly sets a company default via PATCH /admin/tenant-config --
that's the whole mechanism /query uses to detect "not configured yet" and
return a safe error instead of silently calling a vendor GuardRail chose.

The migration/model code was never updated when the architecture doc was
corrected in a prior session -- this brings schema in line with the design
before Phase 3 starts creating tenants through it.

No data migration needed: Phase 2 seed data is demo-only and gets re-seeded
on a fresh volume; existing 'groq' rows are not meaningful information worth
preserving, they're an artifact of the bug this migration fixes.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-30

"""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "tenants", "llm_provider",
        existing_type=sa.String(64),
        nullable=True,
        server_default=None,
    )
    op.alter_column(
        "tenants", "llm_model_name",
        existing_type=sa.String(128),
        nullable=True,
        server_default=None,
    )


def downgrade() -> None:
    # Restores the OLD (incorrect) behavior. Only present for alembic's
    # symmetry convention -- re-applying the pre-correction Groq default is
    # not something this project should actually do; if you're running this,
    # you're intentionally reverting the architecture correction, not just
    # the schema change.
    op.execute("UPDATE tenants SET llm_provider = 'groq' WHERE llm_provider IS NULL")
    op.execute("UPDATE tenants SET llm_model_name = 'llama-3.1-8b-instant' WHERE llm_model_name IS NULL")
    op.alter_column(
        "tenants", "llm_provider",
        existing_type=sa.String(64),
        nullable=False,
        server_default="groq",
    )
    op.alter_column(
        "tenants", "llm_model_name",
        existing_type=sa.String(128),
        nullable=False,
        server_default="llama-3.1-8b-instant",
    )
