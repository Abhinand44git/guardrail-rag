"""
Phase 2, decision log item #2: "Write a unit test with one fixture user per
role, one fixture doc per sensitivity level, before building any endpoint
that uses the query -- this comparison silently over-shares if flipped,
with no errors thrown. This is the single highest-consequence bug surface
in the system."

Verifies app.rbac.visible_document_vectors_query against a real Postgres +
pgvector instance -- this is a raw SQL comparison, so the only test that
actually proves it is one that runs it. Confirms:
  1. Admin sees everything, Developer sees min_role 2-3, Trainee sees only 3
     (the role-clearance direction is correct, not accidentally inverted).
  2. No role, however high its clearance, ever sees another tenant's
     documents (tenant isolation holds independently of the role check).
"""
import uuid

from app.models import Document, DocumentVector, Role, Tenant, User
from app.rbac import visible_document_vectors_query

ZERO_VECTOR = [0.0] * 768


async def _build_tenant_with_fixtures(db, name: str) -> dict:
    """One fixture user per role, one fixture doc (+ one chunk) per sensitivity level."""
    tenant = Tenant(id=uuid.uuid4(), name=name)
    db.add(tenant)
    await db.flush()

    users, docs = {}, {}
    for role in Role:
        users[role] = User(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            username=f"{role.name.lower()}-{name}",
            email=f"{role.name.lower()}@{name}.test",
            hashed_password="x",
            role=role.value,
        )
        db.add(users[role])

        docs[role] = Document(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            title=f"{role.name}-doc",
            # Phase 4: source_type is now required (migration 0010). These
            # fixtures are RBAC-only docs, not real ingested content --
            # 'markdown' is an arbitrary but valid choice, same reasoning as
            # scripts/seed.py's fixtures.
            source_type="markdown",
            min_role_required=role.value,
        )
        db.add(docs[role])

    await db.flush()

    for role in Role:
        db.add(DocumentVector(
            id=uuid.uuid4(),
            document_id=docs[role].id,
            tenant_id=tenant.id,
            chunk_text=f"chunk for {role.name}",
            embedding=ZERO_VECTOR,
            embedding_model="test-fixture",
            embedding_dims=768,
            # Phase 4: chunk_strategy is now required (migration 0011).
            chunk_strategy="test-fixture",
        ))
    await db.flush()

    return {"tenant": tenant, "users": users, "docs": docs}


async def test_admin_sees_every_sensitivity_level(db_session):
    fx = await _build_tenant_with_fixtures(db_session, f"acme-{uuid.uuid4().hex[:8]}")
    result = await db_session.execute(
        visible_document_vectors_query(fx["tenant"].id, Role.ADMIN.value)
    )
    chunks = {row.chunk_text for row in result.scalars().all()}
    assert chunks == {"chunk for ADMIN", "chunk for DEVELOPER", "chunk for TRAINEE"}


async def test_developer_sees_developer_and_trainee_only(db_session):
    fx = await _build_tenant_with_fixtures(db_session, f"acme-{uuid.uuid4().hex[:8]}")
    result = await db_session.execute(
        visible_document_vectors_query(fx["tenant"].id, Role.DEVELOPER.value)
    )
    chunks = {row.chunk_text for row in result.scalars().all()}
    assert chunks == {"chunk for DEVELOPER", "chunk for TRAINEE"}
    assert "chunk for ADMIN" not in chunks


async def test_trainee_sees_trainee_only(db_session):
    fx = await _build_tenant_with_fixtures(db_session, f"acme-{uuid.uuid4().hex[:8]}")
    result = await db_session.execute(
        visible_document_vectors_query(fx["tenant"].id, Role.TRAINEE.value)
    )
    chunks = {row.chunk_text for row in result.scalars().all()}
    assert chunks == {"chunk for TRAINEE"}


async def test_clearance_direction_is_not_accidentally_inverted(db_session):
    """The comparison is `min_role_required >= user_role_number`. If someone
    flips it to <=, Trainees would suddenly see everything and Admins would
    see almost nothing -- assert the row *counts* per role explicitly so an
    inversion fails loudly here instead of silently in production."""
    fx = await _build_tenant_with_fixtures(db_session, f"acme-{uuid.uuid4().hex[:8]}")
    counts = {}
    for role in Role:
        result = await db_session.execute(
            visible_document_vectors_query(fx["tenant"].id, role.value)
        )
        counts[role] = len(result.scalars().all())

    assert counts[Role.ADMIN] == 3
    assert counts[Role.DEVELOPER] == 2
    assert counts[Role.TRAINEE] == 1
    assert counts[Role.ADMIN] > counts[Role.DEVELOPER] > counts[Role.TRAINEE]


async def test_admin_never_sees_another_tenants_documents(db_session):
    """Highest clearance in tenant A must still be zero clearance for
    tenant B's data -- tenant isolation is a separate predicate from the
    role check, and both have to hold independently."""
    fx_a = await _build_tenant_with_fixtures(db_session, f"acme-{uuid.uuid4().hex[:8]}")
    fx_b = await _build_tenant_with_fixtures(db_session, f"globex-{uuid.uuid4().hex[:8]}")

    result = await db_session.execute(
        visible_document_vectors_query(fx_a["tenant"].id, Role.ADMIN.value)
    )
    rows = result.scalars().all()
    tenant_ids_seen = {row.tenant_id for row in rows}

    assert tenant_ids_seen == {fx_a["tenant"].id}
    assert fx_b["tenant"].id not in tenant_ids_seen
    assert len(rows) == 3  # only tenant A's 3 chunks, none of tenant B's
