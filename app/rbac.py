"""
The core RBAC visibility rule the whole security model rests on (see
decision log, "The one query the security model rests on"). Centralized
here as the single source of truth: Phase 5's /retrieve endpoint imports
visible_document_vectors_query() rather than re-deriving this comparison,
so there is exactly one place this logic can be gotten wrong -- and it's
the one place with dedicated test coverage (tests/test_rbac.py).

Lower role number = higher clearance:
  Admin (1)     sees documents with min_role_required IN (1, 2, 3) -- everything
  Developer (2) sees documents with min_role_required IN (2, 3)
  Trainee (3)   sees documents with min_role_required IN (3)       -- least

Equivalent to the raw SQL from the decision log:
  WHERE dv.tenant_id = :tenant_id AND dm.min_role_required >= :user_role_number
"""
import uuid

from sqlalchemy import Select, func, or_, select

from app.models import Document, DocumentVector, ProjectMemory


def visible_document_vectors_query(tenant_id: uuid.UUID, user_role: int) -> Select:
    """
    document_vectors rows a user of the given role, in the given tenant, is
    allowed to see. Tenant isolation and role-based sensitivity filtering
    both happen in this one query -- get either predicate wrong and it
    silently over-shares, with no error thrown. Callers still need to add
    vector similarity ordering / limits (Phase 5); this only encodes the
    access-control predicate.
    """
    return (
        select(DocumentVector)
        .join(Document, DocumentVector.document_id == Document.id)
        .where(DocumentVector.tenant_id == tenant_id)
        .where(Document.min_role_required >= user_role)
    )


def visible_memory_query(tenant_id: uuid.UUID, user_role: int, user_id: uuid.UUID) -> Select:
    """
    project_memory rows this person may see (ADR-0020, amended by ADR-0023):

        same tenant   AND   ( clearance allows it   OR   they wrote it )

    The clearance half is the same rule documents use. The authorship half exists so a person can
    always read back what they saved, even at the Admin-only default. The tenant condition is
    OUTSIDE the OR and therefore applies to both halves: "they wrote it" never reaches across
    tenants. (The easy mistake is writing `tenant AND clearance OR author`, which SQL reads as
    `(tenant AND clearance) OR author` and would let an author see an entry in another tenant;
    tests/test_retrieve_endpoint.py attacks exactly that.) Nothing else decides access: not
    project_id, not the SDLC phase. Those are labels that narrow results, never widen them.
    """
    return (
        select(ProjectMemory)
        .where(ProjectMemory.tenant_id == tenant_id)
        .where(or_(
            ProjectMemory.min_role_required >= user_role,
            ProjectMemory.created_by_user_id == user_id,
        ))
    )


# ---------------------------------------------------------------------------
# Ranked retrieval (Phase 5, ADR-0010). Each query below STARTS from the access predicate above
# and only ever adds conditions to it (AND), so a ranking or label filter can narrow what a
# user sees but can never widen it. What they add:
#   * embedding_model = :model  -- vectors from different models are not comparable (ADR-0005)
#   * ORDER BY cosine distance  -- never L2 or inner product: stored vectors are not unit length
#     (ADR-0010), so those would rank differently and worse
#   * vector_norm > 0           -- cosine distance to an all-zero vector is NaN, which is not
#     valid JSON; real model output is never all-zero, but a fixture or a bad row must not be
#     able to break a response
# Similarity score = 1 - cosine distance (the number the evaluation reported).
# ---------------------------------------------------------------------------
def ranked_document_chunks_query(
    tenant_id: uuid.UUID,
    user_role: int,
    query_embedding: list[float],
    embedding_model: str,
    limit: int,
) -> Select:
    distance = DocumentVector.embedding.cosine_distance(query_embedding)
    return (
        visible_document_vectors_query(tenant_id, user_role)
        .add_columns(Document.title, Document.source_type, distance.label("distance"))
        .where(DocumentVector.embedding_model == embedding_model)
        .where(func.vector_norm(DocumentVector.embedding) > 0)
        .order_by(distance, DocumentVector.id)
        .limit(limit)
    )


def ranked_memory_query(
    tenant_id: uuid.UUID,
    user_role: int,
    user_id: uuid.UUID,
    query_embedding: list[float],
    embedding_model: str,
    limit: int,
    project_id: uuid.UUID | None = None,
    phases: list[str] | None = None,
) -> Select:
    distance = ProjectMemory.embedding.cosine_distance(query_embedding)
    query = (
        visible_memory_query(tenant_id, user_role, user_id)
        .add_columns(distance.label("distance"))
        .where(ProjectMemory.embedding_model == embedding_model)
        .where(func.vector_norm(ProjectMemory.embedding) > 0)
    )
    if project_id is not None:
        query = query.where(ProjectMemory.project_id == project_id)  # a label filter, not security
    if phases is not None:
        query = query.where(ProjectMemory.sdlc_phase.in_(phases))
    return query.order_by(distance, ProjectMemory.id).limit(limit)
