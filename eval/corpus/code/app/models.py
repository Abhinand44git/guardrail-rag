import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, DateTime, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Role(int, enum.Enum):
    """Lower number = higher clearance. Fixed roles only, no custom roles."""
    ADMIN = 1
    DEVELOPER = 2
    TRAINEE = 3


def uuid_pk():
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Operator(Base):
    """
    Global identity plane. Deliberately has NO tenant_id column anywhere —
    structural isolation from tenant data, not a permission check.
    """
    __tablename__ = "operators"

    id: Mapped[uuid.UUID] = uuid_pk()
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = uuid_pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # Admin cap — enforced by DB trigger trg_enforce_max_admins, see migration 0002.
    max_admins: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # Tenant lifecycle — set by the Operator plane (Phase 3). See migration 0005.
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_by_operator_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("operators.id"), nullable=True
    )


    # Generation model config — per-tenant, no schema impact on vectors.
    # All three start NULL and stay NULL until an Admin sets them via
    # PATCH /admin/tenant-config. GuardRail owns no generation model and
    # has no default to fall back to -- /query must return a safe
    # "not configured yet" error for a tenant with nulls here, not
    # silently pick a vendor on the tenant's behalf. See migration 0004.
    llm_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    llm_model_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Fernet-encrypted ciphertext, never plaintext. NULL when no company
    # default is configured for this tenant (not "using a GuardRail default").
    llm_api_key_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("max_admins >= 1 AND max_admins <= 5", name="ck_tenants_max_admins_range"),
    )

    users: Mapped[list["User"]] = relationship(back_populates="tenant", cascade="all, delete-orphan")


class User(Base):
    """Tenant-scoped identity plane. One user = one tenant = one role."""
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    # Globally unique (not just per-tenant): /auth/login looks a user up by
    # email alone, with no tenant identifier in the request, so it relies on
    # email resolving to exactly one row across the whole table. Phase 1
    # shipped this without a DB-level constraint -- found via Phase 2 testing
    # when two same-email rows made login raise MultipleResultsFound instead
    # of authenticating. See migration 0003.
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[int] = mapped_column(Integer, nullable=False)  # 1=Admin, 2=Developer, 3=Trainee

    # Soft deactivation (Phase 3.5, migration 0007) -- mirrors tenants.is_active,
    # same precedent. DELETE /admin/users sets this to false rather than removing
    # the row: project_memory.created_by_user_id and query_log.user_id both need
    # to keep resolving to a real user to preserve "who decided/asked what," which
    # is the entire point of the SDLC-memory feature. A deactivated Admin's slot
    # is freed against tenants.max_admins -- see migration 0008's trigger update.
    is_active: Mapped[bool] = mapped_column(nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("role IN (1, 2, 3)", name="ck_users_role_fixed"),
        UniqueConstraint("tenant_id", "username", name="uq_users_tenant_username"),
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")


class Document(Base):
    """Metadata table — referenced as `dm` in the core RBAC query."""
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)

    # Phase 4, migration 0010: specified in the Design doc's schema from the
    # start (document_metadata.source_type) but never actually migrated in —
    # found while verifying the decision log's claims before building
    # ingestion on top of this table. Ingestion (app/routers/ingest.py) sets
    # this on every new row; pre-Phase-4 rows were backfilled, see 0010.
    source_type: Mapped[str] = mapped_column(String(16), nullable=False)

    # Fail-closed default, changed from TRAINEE(3) to ADMIN(1) this phase
    # (decision log, Session 5 addendum #3): a newly uploaded document is
    # Admin-only until an Admin deliberately loosens it via
    # PATCH /ingest/document/{id} — matches the project's existing reasoning
    # for keeping reclassification Admin-only, applied one step earlier, at
    # creation. See migration 0010 for the matching DB-level default change.
    min_role_required: Mapped[int] = mapped_column(Integer, nullable=False, default=Role.ADMIN.value)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("min_role_required IN (1, 2, 3)", name="ck_documents_min_role_fixed"),
        CheckConstraint("source_type IN ('code','pdf','markdown')", name="ck_documents_source_type"),
    )


class DocumentVector(Base):
    """
    Chunk-level embeddings — referenced as `dv` in the core RBAC query.
    tenant_id is denormalized here (not just via document join) so the
    core query can filter on dv.tenant_id directly.
    """
    __tablename__ = "document_vectors"

    id: Mapped[uuid.UUID] = uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    chunk_text: Mapped[str] = mapped_column(nullable=False)

    # Widened to 1536, zero-padded for smaller/narrower models (e.g.
    # nomic-embed-text-v1.5's native 768 dims).
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dims: Mapped[int] = mapped_column(Integer, nullable=False)

    # Phase 4, migration 0011: same situation as Document.source_type above —
    # specified in the Design doc's schema, discussed in the decision log as
    # if it already existed ("chunk_strategy is already a per-row column"),
    # but never actually migrated in. Free text, not a fixed enum, by design:
    # the whole point is letting the strategy change over time (e.g.
    # fixed_500_tokens now, something smarter later) without a migration
    # touching rows written under the old one. See app/chunking.py.
    chunk_strategy: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProjectMemory(Base):
    """
    SDLC-phase-aware project memory (decisions/notes saved across the
    lifecycle, retrieved alongside document chunks in Phase 5's dual RAG).

    Schema-only this phase (migration 0009) -- the POST/GET /project-memory
    endpoints themselves are not part of Phase 4's stated scope (Ingestion
    covers documents, not decisions) and are not built here; see the
    accompanying write-up for that boundary call. Created now, rather than
    left for whichever phase builds those endpoints, because the decision
    log's Session 5 addendum explicitly locked this table's shape as a
    Phase 4 deliverable (item #1) -- bundled with the embedding-model
    propagation work since both tables' embedding columns needed the same
    fix at the same time.

    The table did not previously exist in the shipped schema at all, despite
    the decision log discussing it as something to "widen" from VECTOR(384)
    -- verified against the actual code before writing migration 0009; see
    the accompanying write-up. Built straight at VECTOR(1536) since there
    was never a narrower version of this table to migrate from.

    embedding_model and embedding_dims were both added here even though the
    Session 5 addendum's summary line only named embedding_dims explicitly
    -- its own stated reasoning ("one consistent embedding-storage design
    across both tables, not two different ones") only holds if this table
    carries the same pattern document_vectors does. Flagged as an
    extrapolation from that stated reasoning, not a separate decision made
    silently.

    No chunk_strategy here, unlike document_vectors: a project-memory entry
    is one saved note embedded as a single row, not a document split into
    many chunk rows, so "how was this split up" doesn't apply the same way.
    """
    __tablename__ = "project_memory"

    id: Mapped[uuid.UUID] = uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    sdlc_phase: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)

    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    embedding_model: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_dims: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "sdlc_phase IN ('planning','analysis','design','development','testing','deployment')",
            name="ck_project_memory_sdlc_phase",
        ),
    )


class TokenBlacklist(Base):
    """
    Phase 2's own build-phase description (GuardRail_RAG.md: "Security
    core: ...JWT plane separation, token blacklist") promised this; it was
    never actually built -- no table, no /auth/logout, no jti claim even
    minted -- found during an audit of the original Phase 2 deliverable,
    not just the current codebase. Fixed here, several phases later than it
    should have been.

    token_jti alone is the primary key, matching the Design doc's schema
    exactly (no composite with tenant_id): a jti is already globally
    unique on its own (uuid4, minted fresh per token in
    app/security.py::_encode()), operator tokens included.
    """
    __tablename__ = "token_blacklist"

    token_jti: Mapped[str] = mapped_column(String(36), primary_key=True)

    # Nullable on purpose, matching the Design doc's schema exactly
    # (`tenant_id UUID REFERENCES tenants(id)`, no NOT NULL): operator
    # tokens carry no tenant_id claim at all, so a blacklisted operator
    # token's row has nothing to put here.
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True
    )
    blacklisted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # Mirrors the token's own exp claim. Not read by the blacklist check
    # itself (decode_token() already rejects a naturally-expired token via
    # its signature-level exp check, before the blacklist is ever
    # consulted) -- stored so a future cleanup job could prune rows whose
    # underlying token would already be unusable on its own, without
    # needing to re-decode anything to find out which rows those are. No
    # such job is built yet -- not asked for, same category as
    # app/crypto.py's documented key-rotation gap.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
