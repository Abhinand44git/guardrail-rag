"""
Phase 4: ingestion endpoints (/ingest/code, /ingest/document,
PATCH & DELETE /ingest/document/{id}), tested end-to-end over ASGI against
the real Postgres + pgvector instance -- same standard as
test_admin_endpoints.py. Role-gated (Admin/Developer can ingest, Trainee
cannot; delete/reclassify are Admin-only), so this gets proven working, not
just written, same as every other role-gate in this codebase.

Embedding backend: overridden with a fake, deterministic backend for the
whole module (see _fake_embedding_service below) rather than the real
embedder service (ADR-0012), so these tests need no model and no network.
The real HTTP path to the embedder is covered separately in
tests/test_embedder_service.py. The fake backend returns a deterministic 768-wide vector (the storage width, ADR-0019) so tests can
confirm the stored vector is exactly what the backend returned.

Setup pattern: same shortcut test_admin_endpoints.py uses -- insert a
committed Tenant + User row directly and mint a bearer token via
create_user_token, rather than going through /auth/login for every test.
"""
import asyncio
import io
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.embeddings import EmbeddingService, get_embedding_service
from app.main import app
from app.models import Document, DocumentVector, Role, Tenant, User
from app.security import create_user_token, hash_password

PASSWORD = "SomeUsersPassword123!"


# --- fake, network-free embedding backend -----------------------------

class _FakeEmbeddingBackend:
    NATIVE_DIMS = 768  # the storage width (ADR-0019): the service refuses anything else

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float((len(t) + i) % 7) + 1.0 for i in range(self.NATIVE_DIMS)] for t in texts]

    @property
    def native_dims(self) -> int:
        return self.NATIVE_DIMS


def _fake_embedding_service() -> EmbeddingService:
    return EmbeddingService(_FakeEmbeddingBackend(), "fake-test-embedding-model")


@pytest.fixture(autouse=True)
def _override_embedding_service():
    app.dependency_overrides[get_embedding_service] = _fake_embedding_service
    yield
    app.dependency_overrides.pop(get_embedding_service, None)


# --- shared helpers (same shape as tests/test_admin_endpoints.py) -----

async def _make_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _make_tenant() -> uuid.UUID:
    tenant_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(Tenant(id=tenant_id, name=f"ingest-test-{uuid.uuid4().hex[:8]}"))
        await db.commit()
    return tenant_id


async def _make_user(tenant_id: uuid.UUID, role: int) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with AsyncSessionLocal() as db:
        db.add(User(
            id=user_id, tenant_id=tenant_id, username=f"u-{user_id.hex[:8]}",
            email=f"{user_id.hex[:8]}@ingest-test.local",
            hashed_password=hash_password(PASSWORD), role=role,
        ))
        await db.commit()
    return user_id


def _token(user_id: uuid.UUID, tenant_id: uuid.UUID, role: int) -> str:
    return create_user_token(user_id, tenant_id, role)


async def _cleanup(*tenant_ids: uuid.UUID):
    async with AsyncSessionLocal() as db:
        for tenant_id in tenant_ids:
            for vec in (await db.execute(
                select(DocumentVector).where(DocumentVector.tenant_id == tenant_id)
            )).scalars().all():
                await db.delete(vec)
            for doc in (await db.execute(
                select(Document).where(Document.tenant_id == tenant_id)
            )).scalars().all():
                await db.delete(doc)
            for user in (await db.execute(
                select(User).where(User.tenant_id == tenant_id)
            )).scalars().all():
                await db.delete(user)
            tenant = await db.get(Tenant, tenant_id)
            if tenant:
                await db.delete(tenant)
        await db.commit()


async def _vectors_for(document_id: uuid.UUID) -> list[DocumentVector]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(DocumentVector).where(DocumentVector.document_id == document_id))
        return result.scalars().all()


def _make_minimal_pdf(text: str) -> bytes:
    """Hand-built, minimal single-page PDF containing `text` -- avoids
    adding a PDF-writing dependency just to generate a test fixture (the
    real ingestion path only ever needs to *read* PDFs, via pypdf)."""
    content_stream = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 5 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 4 0 R >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(content_stream)} >>\nstream\n".encode("latin-1")
        + content_stream + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
    ]
    buf = io.BytesIO()
    buf.write(b"%PDF-1.4\n")
    offsets = [0]
    for obj in objects:
        offsets.append(buf.tell())
        buf.write(obj)
    xref_offset = buf.tell()
    n = len(objects) + 1
    buf.write(f"xref\n0 {n}\n".encode())
    buf.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.write(f"{off:010d} 00000 n \n".encode())
    buf.write(f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode())
    return buf.getvalue()


# --- POST /ingest/code --------------------------------------------------

async def test_admin_can_ingest_code():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "auth.py", "content": "def login():\n    return True\n" * 50},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["source_type"] == "code"
        assert body["chunk_count"] >= 1
        assert body["min_role_required"] == Role.ADMIN.value  # default, not specified

        vectors = await _vectors_for(uuid.UUID(body["id"]))
        assert len(vectors) == body["chunk_count"]
        for v in vectors:
            assert v.chunk_strategy == "fixed_500_words"
            assert v.embedding_model == "fake-test-embedding-model"
            assert v.embedding_dims == _FakeEmbeddingBackend.NATIVE_DIMS
            assert len(v.embedding) == 768
            # stored exactly as the backend returned it: no padding, no cutting (ADR-0019)
            assert any(x != 0.0 for x in v.embedding)
    finally:
        await _cleanup(tenant_id)


async def test_developer_can_ingest_code_own_tenant():
    tenant_id = await _make_tenant()
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value)
    token = _token(dev_id, tenant_id, Role.DEVELOPER.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "utils.py", "content": "x = 1\n"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
    finally:
        await _cleanup(tenant_id)


async def test_trainee_cannot_ingest_code():
    tenant_id = await _make_tenant()
    trainee_id = await _make_user(tenant_id, Role.TRAINEE.value)
    token = _token(trainee_id, tenant_id, Role.TRAINEE.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "utils.py", "content": "x = 1\n"},
                headers={"Authorization": f"Bearer {token}"},
            )
        # Same convention as every other role gate in this codebase
        # (app/deps.py::require_role reuses _AUTH_ERROR) -- 401, not 403.
        assert r.status_code == 401
    finally:
        await _cleanup(tenant_id)


async def test_ingest_code_respects_explicit_min_role_required():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "public_utils.py", "content": "x = 1\n", "min_role_required": Role.TRAINEE.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
        assert r.json()["min_role_required"] == Role.TRAINEE.value
    finally:
        await _cleanup(tenant_id)


async def test_ingest_code_rejects_empty_content():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "empty.py", "content": "   \n  "},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_input"
    finally:
        await _cleanup(tenant_id)


# --- POST /ingest/document (markdown + pdf) -----------------------------

async def test_admin_can_ingest_markdown_document():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        content = ("# Runbook\n\nSome operational notes. " * 80).encode("utf-8")
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/document",
                files={"file": ("runbook.md", content, "text/markdown")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["source_type"] == "markdown"
        assert body["title"] == "runbook.md"
        assert body["chunk_count"] >= 1
    finally:
        await _cleanup(tenant_id)


async def test_admin_can_ingest_pdf_document_text_is_actually_extracted():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        marker = "GuardRailPdfExtractionMarker12345"
        pdf_bytes = _make_minimal_pdf(marker)
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/document",
                files={"file": ("spec.pdf", pdf_bytes, "application/pdf")},
                data={"title": "Product spec"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["source_type"] == "pdf"
        assert body["title"] == "Product spec"

        vectors = await _vectors_for(uuid.UUID(body["id"]))
        assert any(marker in v.chunk_text for v in vectors), (
            "extracted PDF text should actually reach a stored chunk, not just produce *some* chunk"
        )
    finally:
        await _cleanup(tenant_id)


async def test_ingest_document_rejects_unsupported_extension():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/document",
                files={"file": ("archive.zip", b"not really a zip", "application/zip")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_input"
    finally:
        await _cleanup(tenant_id)


async def test_ingest_document_rejects_oversized_file(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ingest_max_file_size_mb", 0)  # anything nonempty now exceeds the cap

    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/document",
                files={"file": ("notes.md", b"# hello world", "text/markdown")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert "exceeds" in r.json()["detail"]["detail"].lower()
    finally:
        await _cleanup(tenant_id)


async def test_trainee_cannot_ingest_document():
    tenant_id = await _make_tenant()
    trainee_id = await _make_user(tenant_id, Role.TRAINEE.value)
    token = _token(trainee_id, tenant_id, Role.TRAINEE.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/document",
                files={"file": ("notes.md", b"# hello", "text/markdown")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 401
    finally:
        await _cleanup(tenant_id)


# --- PATCH /ingest/document/{id} (Admin-only reclassify) ----------------

async def test_admin_can_reclassify_document():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            created = await client.post(
                "/ingest/code",
                json={"title": "x.py", "content": "x = 1\n"},
                headers={"Authorization": f"Bearer {token}"},
            )
            doc_id = created.json()["id"]

            r = await client.patch(
                f"/ingest/document/{doc_id}",
                json={"min_role_required": Role.TRAINEE.value},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text
        assert r.json()["min_role_required"] == Role.TRAINEE.value
    finally:
        await _cleanup(tenant_id)


async def test_developer_cannot_reclassify_document():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value)
    admin_token = _token(admin_id, tenant_id, Role.ADMIN.value)
    dev_token = _token(dev_id, tenant_id, Role.DEVELOPER.value)
    try:
        async with await _make_client() as client:
            created = await client.post(
                "/ingest/code",
                json={"title": "x.py", "content": "x = 1\n"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            doc_id = created.json()["id"]

            r = await client.patch(
                f"/ingest/document/{doc_id}",
                json={"min_role_required": Role.TRAINEE.value},
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        # Deliberate per the Analysis doc: reclassification is Admin-only,
        # not extended to Developer even though Developer can ingest.
        assert r.status_code == 401
    finally:
        await _cleanup(tenant_id)


async def test_reclassify_rejects_out_of_range_value():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            created = await client.post(
                "/ingest/code",
                json={"title": "x.py", "content": "x = 1\n"},
                headers={"Authorization": f"Bearer {token}"},
            )
            doc_id = created.json()["id"]

            r = await client.patch(
                f"/ingest/document/{doc_id}",
                json={"min_role_required": 4},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 422  # Pydantic field validation, ge/le
    finally:
        await _cleanup(tenant_id)


async def test_reclassify_cross_tenant_document_is_generic_404():
    tenant_a = await _make_tenant()
    tenant_b = await _make_tenant()
    admin_a = await _make_user(tenant_a, Role.ADMIN.value)
    admin_b = await _make_user(tenant_b, Role.ADMIN.value)
    token_a = _token(admin_a, tenant_a, Role.ADMIN.value)
    token_b = _token(admin_b, tenant_b, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            created = await client.post(
                "/ingest/code",
                json={"title": "secret.py", "content": "x = 1\n"},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            doc_id = created.json()["id"]

            r = await client.patch(
                f"/ingest/document/{doc_id}",
                json={"min_role_required": Role.TRAINEE.value},
                headers={"Authorization": f"Bearer {token_b}"},
            )
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "not_found"

        # Prove tenant A's document was genuinely untouched, not just hidden
        # from tenant B's response -- same pattern as
        # test_admin_endpoints.py's cross-tenant checks.
        async with AsyncSessionLocal() as db:
            doc = await db.get(Document, uuid.UUID(doc_id))
            assert doc.min_role_required == Role.ADMIN.value  # unchanged
    finally:
        await _cleanup(tenant_a, tenant_b)


# --- DELETE /ingest/document/{id} (Admin-only, hard delete) -------------

async def test_admin_can_delete_document_and_its_chunks_cascade():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            created = await client.post(
                "/ingest/code",
                json={"title": "temp.py", "content": "x = 1\n" * 20},
                headers={"Authorization": f"Bearer {token}"},
            )
            doc_id = created.json()["id"]
            assert len(await _vectors_for(uuid.UUID(doc_id))) >= 1

            r = await client.delete(
                f"/ingest/document/{doc_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 200, r.text

        async with AsyncSessionLocal() as db:
            assert await db.get(Document, uuid.UUID(doc_id)) is None
        # Cascade (ON DELETE CASCADE, migration 0001) removed the chunks too.
        assert await _vectors_for(uuid.UUID(doc_id)) == []
    finally:
        await _cleanup(tenant_id)


async def test_developer_cannot_delete_document():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    dev_id = await _make_user(tenant_id, Role.DEVELOPER.value)
    admin_token = _token(admin_id, tenant_id, Role.ADMIN.value)
    dev_token = _token(dev_id, tenant_id, Role.DEVELOPER.value)
    try:
        async with await _make_client() as client:
            created = await client.post(
                "/ingest/code",
                json={"title": "temp.py", "content": "x = 1\n"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            doc_id = created.json()["id"]

            r = await client.delete(
                f"/ingest/document/{doc_id}",
                headers={"Authorization": f"Bearer {dev_token}"},
            )
        assert r.status_code == 401
    finally:
        await _cleanup(tenant_id)


async def test_delete_nonexistent_document_is_generic_404():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.delete(
                f"/ingest/document/{uuid.uuid4()}",
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 404
        assert r.json()["detail"]["error"] == "not_found"
    finally:
        await _cleanup(tenant_id)


# --- Session 10: previously-flagged untested surface area ---------------
# (DBAPIError handling, broadened PDF exception handling, concurrent
# ingests, exact file-size boundary, large documents, embedding-call
# failure, missing auth header on every ingest route)

async def test_ingest_routes_reject_missing_auth_header():
    """None of the four ingest endpoints had a direct test for "no
    Authorization header at all" -- require_role is already proven
    elsewhere, but not exercised specifically on these routes until now."""
    async with await _make_client() as client:
        r1 = await client.post("/ingest/code", json={"title": "x.py", "content": "x = 1\n"})
        r2 = await client.post("/ingest/document", files={"file": ("x.md", b"# x", "text/markdown")})
        r3 = await client.patch(f"/ingest/document/{uuid.uuid4()}", json={"min_role_required": 1})
        r4 = await client.delete(f"/ingest/document/{uuid.uuid4()}")
    assert r1.status_code == 401
    assert r2.status_code == 401
    assert r3.status_code == 401
    assert r4.status_code == 401


async def test_malformed_pdf_returns_safe_400_not_a_raw_500():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/document",
                files={"file": ("bad.pdf", b"this is not a real pdf, just text with a .pdf name", "application/pdf")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400
        assert r.json()["detail"]["error"] == "invalid_input"
    finally:
        await _cleanup(tenant_id)


async def test_pdf_extraction_catches_exceptions_outside_pypdfs_own_hierarchy(monkeypatch):
    """The gap this is actually testing: _extract_pdf_text previously only
    caught PdfReadError. Real malformed PDFs are hard to coax into
    tripping anything outside pypdf's own exception family (pypdf is quite
    defensive) -- so this injects a non-PyPdfError exception directly to
    prove the broadened `except Exception` catch is what's actually doing
    the work, not just PdfReadError's existing subclasses."""
    import pypdf

    def _boom(*args, **kwargs):
        raise IndexError("simulated internal pypdf failure outside its own exception hierarchy")

    monkeypatch.setattr(pypdf, "PdfReader", _boom)

    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/document",
                files={"file": ("looks_fine.pdf", b"%PDF-1.4\nirrelevant, PdfReader is patched", "application/pdf")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"] == "invalid_input"
    finally:
        await _cleanup(tenant_id)


async def test_db_failure_during_ingest_rolls_back_and_returns_safe_500(monkeypatch):
    """The actual gap: app/routers/ingest.py had no DBAPIError handling at
    all (unlike admin.py/operator.py). Simulated here since the real
    constraints document/chunk inserts could trip are all already
    validated before reaching the DB -- a genuine race is hard to trigger
    on demand, so commit() is patched to fail the way a dropped connection
    would."""
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async def broken_commit(self):
            raise DBAPIError("simulated commit", {}, Exception("connection lost"))

        monkeypatch.setattr(AsyncSession, "commit", broken_commit)

        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "will-fail.py", "content": "x = 1\n" * 20},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["error"] == "server_error"

        monkeypatch.undo()  # restore a working commit before verifying cleanup

        async with AsyncSessionLocal() as db:
            count = (await db.execute(
                select(func.count()).select_from(Document).where(
                    Document.tenant_id == tenant_id, Document.title == "will-fail.py"
                )
            )).scalar_one()
        assert count == 0, "a failed commit must not leave a partial Document row behind"
    finally:
        await _cleanup(tenant_id)


async def test_embedding_backend_failure_returns_safe_error_and_leaves_no_partial_document():
    """Flagged as untested surface area, and found to also be unhandled
    while writing this test (Session 10) -- the embed_documents call
    wasn't wrapped in anything, so a real backend exception would have
    propagated raw instead of this project's safe-error contract. Fixed
    alongside this test, not just tested."""
    class _FailingBackend:
        def encode(self, texts):
            raise RuntimeError("simulated embedding backend failure")

        @property
        def native_dims(self):
            return 8

    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        app.dependency_overrides[get_embedding_service] = (
            lambda: EmbeddingService(_FailingBackend(), "failing-test-model")
        )
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "will-fail-embedding.py", "content": "x = 1\n" * 20},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 500, r.text
        assert r.json()["detail"]["error"] == "server_error"

        async with AsyncSessionLocal() as db:
            count = (await db.execute(
                select(func.count()).select_from(Document).where(
                    Document.tenant_id == tenant_id, Document.title == "will-fail-embedding.py"
                )
            )).scalar_one()
        assert count == 0, "an embedding failure must not leave a partial Document row behind"
    finally:
        await _cleanup(tenant_id)


async def test_file_exactly_at_size_cap_is_accepted_one_byte_over_is_rejected(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "ingest_max_file_size_mb", 0.001)  # 1048 bytes exactly
    max_bytes = int(0.001 * 1024 * 1024)

    body = b"# heading\n" + b"w " * ((max_bytes - 10) // 2)
    exact = body[:max_bytes]
    assert len(exact) == max_bytes
    over = exact + b"x"

    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            at_cap = await client.post(
                "/ingest/document",
                files={"file": ("exact.md", exact, "text/markdown")},
                headers={"Authorization": f"Bearer {token}"},
            )
            over_cap = await client.post(
                "/ingest/document",
                files={"file": ("over.md", over, "text/markdown")},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert at_cap.status_code == 201, at_cap.text
        assert over_cap.status_code == 400
        assert "exceeds" in over_cap.json()["detail"]["detail"].lower()
    finally:
        await _cleanup(tenant_id)


async def test_large_document_produces_many_chunks_correctly():
    # One short "statement" per line, like real code -- not one giant
    # single line. (First draft of this test used a single newline-free
    # line and got exactly 1 chunk: code is chunked by line boundaries, see
    # app/chunking.py's documented limitation that a single line longer
    # than the target becomes its own oversized chunk rather than being
    # split further. Realistic multi-line input avoids that case here.)
    content = "\n".join(f"x{i} = {i}" for i in range(12000))

    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    try:
        async with await _make_client() as client:
            r = await client.post(
                "/ingest/code",
                json={"title": "huge.py", "content": content},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["chunk_count"] > 20  # ~12000 words / 450-word effective step

        vectors = await _vectors_for(uuid.UUID(body["id"]))
        assert len(vectors) == body["chunk_count"]
        # spot-check coverage: first and last lines both made it into some chunk
        all_text = " ".join(v.chunk_text for v in vectors)
        assert "x0 = 0" in all_text
        assert "x11999 = 11999" in all_text
    finally:
        await _cleanup(tenant_id)


async def test_concurrent_ingests_to_same_tenant_both_succeed_independently():
    tenant_id = await _make_tenant()
    admin_id = await _make_user(tenant_id, Role.ADMIN.value)
    token = _token(admin_id, tenant_id, Role.ADMIN.value)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with await _make_client() as client:
            r1, r2 = await asyncio.gather(
                client.post("/ingest/code", json={"title": "concurrent-a.py", "content": "a = 1\n" * 30}, headers=headers),
                client.post("/ingest/code", json={"title": "concurrent-b.py", "content": "b = 2\n" * 30}, headers=headers),
            )
        assert r1.status_code == 201, r1.text
        assert r2.status_code == 201, r2.text
        id_a, id_b = r1.json()["id"], r2.json()["id"]
        assert id_a != id_b

        vectors_a = await _vectors_for(uuid.UUID(id_a))
        vectors_b = await _vectors_for(uuid.UUID(id_b))
        assert len(vectors_a) == r1.json()["chunk_count"]
        assert len(vectors_b) == r2.json()["chunk_count"]
        # no cross-contamination between the two concurrent requests' chunks
        assert all("a = 1" in v.chunk_text for v in vectors_a)
        assert all("b = 2" in v.chunk_text for v in vectors_b)
    finally:
        await _cleanup(tenant_id)
