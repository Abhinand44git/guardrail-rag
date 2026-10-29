"""document_vectors: add chunk_strategy

Same situation as migration 0010's source_type addition: specified in the
Design doc's schema, and discussed in the decision log as if it already
existed ("chunk_strategy is already a per-row column specifically so
strategy can evolve later without touching existing data"), but never
actually migrated into the shipped document_vectors table -- found while
verifying decision-log claims before building Phase 4's ingestion pipeline
on top of this table (see also migrations 0009 and 0010's docstrings for the
other two gaps this same verification pass found).

Free text, not a fixed enum -- that's the entire point of this column per
the decision log's own reasoning: letting the strategy change over time
(e.g. today's fixed_500_tokens, something smarter later) without a
migration touching rows written under an earlier strategy.

Added nullable, backfilled, then locked to NOT NULL -- tests/test_rbac.py
inserts DocumentVector fixture rows directly (a fixed zero-vector, not real
ingested content) and predates this column entirely.

Backfill value 'legacy_pre_phase4': distinct from any real chunking
strategy label ingestion will ever record (which follows the
fixed_<N>_tokens shape, see app/chunking.py), so a future reader can tell
at a glance that a row with this value predates the chunking pipeline
existing at all, rather than mistaking it for a real strategy that was
later renamed or removed.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("document_vectors", sa.Column("chunk_strategy", sa.String(64), nullable=True))
    op.execute("UPDATE document_vectors SET chunk_strategy = 'legacy_pre_phase4' WHERE chunk_strategy IS NULL")
    op.alter_column("document_vectors", "chunk_strategy", nullable=False)


def downgrade() -> None:
    op.drop_column("document_vectors", "chunk_strategy")
