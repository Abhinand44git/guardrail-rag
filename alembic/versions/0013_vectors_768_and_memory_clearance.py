"""Phase 5: vectors 1536 -> 768 (no padding), and project_memory gets a clearance level

Two changes, one migration, both decided together (ADR-0019, ADR-0020):

1. document_vectors.embedding and project_memory.embedding: vector(1536) -> vector(768).
   Every stored vector is nomic's real 768 numbers followed by 768 zeros of padding
   (ADR-0014). Keeping the first 768 loses nothing, so no row needs re-embedding. The
   evaluation (eval/results, --dims run) found nothing to gain from storing fewer than 768.

   SAFETY CHECK, runs first: the migration REFUSES to run if any stored vector has a
   non-zero number anywhere in positions 769-1536. That would mean the row came from a
   wider model, and cutting it would silently destroy real data. It fails loudly instead;
   those rows must be dealt with (re-embedded or deleted) by a person.

   The cut is written with a real[] round-trip, not pgvector's subvector(), because
   subvector() only exists from pgvector 0.7 and this has to run on 0.6 as well as 0.8.

2. project_memory.min_role_required: new, NOT NULL, default 1 (Admin only), CHECK 1..3.
   Same meaning and same fail-closed default as documents.min_role_required (migration
   0010). Existing rows (there should be none outside test fixtures) become Admin-only.

Downgrade pads back to 1536 with zeros (lossless) and drops the clearance column.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-25
"""
from alembic import op
import sqlalchemy as sa

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

_TABLES = ("document_vectors", "project_memory")


def _assert_padding_is_pure_zeros(conn, table: str) -> None:
    bad = conn.execute(sa.text(
        f"SELECT count(*) FROM {table} t WHERE EXISTS ("
        f"  SELECT 1 FROM unnest((t.embedding::real[])[769:1536]) AS x WHERE x <> 0)"
    )).scalar_one()
    if bad:
        raise RuntimeError(
            f"{table}: {bad} row(s) have non-zero values beyond position 768, so they are not "
            "768-wide vectors plus padding. Cutting them would destroy data. Re-embed or delete "
            "those rows first, then run this migration again. Nothing was changed."
        )


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        _assert_padding_is_pure_zeros(conn, table)

    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(768) "
            f"USING ((embedding::real[])[1:768])::vector(768)"
        )

    op.add_column(
        "project_memory",
        sa.Column("min_role_required", sa.Integer, nullable=False, server_default=sa.text("1")),
    )
    op.create_check_constraint(
        "ck_project_memory_min_role_fixed", "project_memory", "min_role_required IN (1, 2, 3)"
    )


def downgrade() -> None:
    op.drop_constraint("ck_project_memory_min_role_fixed", "project_memory", type_="check")
    op.drop_column("project_memory", "min_role_required")

    for table in _TABLES:
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN embedding TYPE vector(1536) "
            f"USING ((embedding::real[] || array_fill(0::real, ARRAY[768]))::vector(1536))"
        )
