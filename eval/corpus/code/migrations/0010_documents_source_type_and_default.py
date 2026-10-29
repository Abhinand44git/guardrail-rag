"""documents: add source_type, change min_role_required default to Admin-only

Two changes, bundled because both touch the same table and both are
Phase 4 ingestion prerequisites:

1. source_type. Specified in the Design doc's schema from the very start
   (document_metadata.source_type TEXT NOT NULL CHECK IN
   ('code','pdf','markdown')) but never actually migrated into the shipped
   `documents` table -- found by grepping the codebase while verifying the
   decision log's claims before building Phase 4's ingestion endpoints on
   top of this table, the same "trust but verify" pass that found
   project_memory missing entirely (migration 0009) and chunk_strategy
   missing from document_vectors (migration 0011). Added here as nullable
   first, backfilled, then locked to NOT NULL + CHECK -- there are
   pre-existing rows (scripts/seed.py's fixture docs, tests/test_rbac.py's
   fixture docs) with no notion of source_type, so a straight NOT NULL add
   would fail against any already-seeded database.

   Backfill value 'markdown': these pre-existing rows are metadata-only
   RBAC/seed fixtures with no real ingested content (no document_vectors
   rows of their own from actual ingestion), so the exact backfilled value
   doesn't affect anything currently reading this column -- 'markdown' was
   picked as the most neutral of the three allowed values, not because it's
   factually correct for these rows.

2. min_role_required's default, 3 (Trainee, most permissive) -> 1 (Admin,
   strictest). Decision log, Session 5 addendum #3: "Fail closed, not open.
   Consistent with the project's already-stated reasoning for keeping
   reclassification Admin-only... same principle, applied one step earlier,
   at creation." app/models.py's Document.min_role_required default was
   updated to match (Role.ADMIN.value); this migration makes the DB-level
   default agree, for any insert that doesn't go through the ORM. Existing
   rows are untouched -- server_default only affects future inserts that
   omit the column, same "doesn't retroactively change past rows" precedent
   as every other default/constraint change in this log (e.g. lowering
   max_admins, migration 0004's Groq-default removal).

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_type", sa.String(16), nullable=True))
    op.execute("UPDATE documents SET source_type = 'markdown' WHERE source_type IS NULL")
    op.alter_column("documents", "source_type", nullable=False)
    op.create_check_constraint(
        "ck_documents_source_type", "documents", "source_type IN ('code','pdf','markdown')",
    )

    op.alter_column("documents", "min_role_required", server_default="1")


def downgrade() -> None:
    op.alter_column("documents", "min_role_required", server_default="3")
    op.drop_constraint("ck_documents_source_type", "documents", type_="check")
    op.drop_column("documents", "source_type")
