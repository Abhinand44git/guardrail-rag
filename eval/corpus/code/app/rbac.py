"""
The core RBAC visibility rule the whole security model rests on (see
decision log, "The one query the security model rests on"). Centralized
here as the single source of truth: Phase 5's /query endpoint should import
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

from sqlalchemy import Select, select

from app.models import Document, DocumentVector


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
