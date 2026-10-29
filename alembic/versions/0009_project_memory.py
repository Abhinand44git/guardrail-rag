"""project_memory: create table (Phase 4)

Decision log, Session 5 addendum item #1, locks this table's shape as a
Phase 4 deliverable -- but written there as "widen project_memory.embedding
from VECTOR(384) to VECTOR(1536)", which assumes the table already exists.
Verified against the actual shipped schema before writing this migration
(per standing instruction to check decision-log claims against real code,
not just trust them): project_memory does not exist anywhere in this
codebase -- not in models.py, not in any earlier migration. Grepped for
"project_memory" across the whole tree and found only comments referencing
future/decided behavior, no table definition. So this migration *creates*
the table, straight at VECTOR(1536) + embedding_dims, rather than widening a
narrower version that was never actually built.

embedding_model and embedding_dims are both new columns here, even though
the Session 5 addendum's one-line summary named only embedding_dims -- its
own stated reasoning ("one consistent embedding-storage design across both
tables, not two different ones") only holds if this table carries
document_vectors' full three-column pattern. Extrapolated from that stated
reasoning; flagged as such (not a separate decision made silently) in
app/models.py's ProjectMemory docstring and in the accompanying write-up.

No chunk_strategy here (unlike document_vectors) -- a project-memory entry
is one saved decision/note embedded as a single row, not a document split
into many chunk rows.

Schema-only this migration: the POST/GET /project-memory endpoints that
would actually write to this table are not part of Phase 4's stated scope
and are not built here -- see the accompanying write-up for that boundary
call.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-27

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sdlc_phase", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id"), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=False),
        sa.Column("embedding_model", sa.String(128), nullable=False),
        sa.Column("embedding_dims", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "sdlc_phase IN ('planning','analysis','design','development','testing','deployment')",
            name="ck_project_memory_sdlc_phase",
        ),
    )
    # tenant_id + project_id together, matching the query shape
    # GET /project-memory?phase=&project= will filter on (Design doc:
    # idx_memory_tenant_project) once that endpoint exists.
    op.create_index("ix_project_memory_tenant_project", "project_memory", ["tenant_id", "project_id"])


def downgrade() -> None:
    op.drop_table("project_memory")
