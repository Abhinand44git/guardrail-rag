"""
Ingestion endpoints (Phase 4): POST /ingest/code, POST /ingest/document,
DELETE /ingest/document/{id}, PATCH /ingest/document/{id}.

Role gate: Admin and Developer can ingest (Analysis §1's role table — "Can
ingest documents": Admin yes, Developer yes/own tenant, Trainee no). Delete
and reclassify are Admin-only (Analysis §1: "Document deletion and
min_role_required updates: Admin-only... to avoid a role that can create
sensitive content accidentally acquiring the power to also delete or
reclassify it").

Pipeline is synchronous end to end (decision log, Session 5 addendum #4):
extract text -> chunk -> embed the whole batch in one model call -> insert
Document + all its DocumentVector rows in one transaction. No partial
document (metadata row with zero/some chunks) is ever left committed if
embedding or the insert fails partway through.
"""
import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chunking import chunk_strategy_label, chunk_text
from app.database import get_db
from app.deps import current_tenant_id, require_role
from app.embeddings import EmbeddingService, get_embedding_service
from app.models import Document, DocumentVector, Role
from app.schemas import CodeIngestRequest, DocumentPatchRequest, DocumentResponse
from app.config import settings

router = APIRouter(prefix="/ingest", tags=["ingest"])

_INGEST_ROLES = (Role.ADMIN.value, Role.DEVELOPER.value)

# Extension -> source_type. Deliberately narrow (Analysis §3: source_type
# CHECK IN ('code','pdf','markdown')) -- an unrecognized extension is
# rejected outright rather than guessed at.
_DOCUMENT_EXTENSIONS = {
    ".pdf": "pdf",
    ".md": "markdown",
    ".markdown": "markdown",
}


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={"error": "invalid_input", "detail": detail},
    )


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "detail": "document not found"},
    )


def _embedding_failed() -> HTTPException:
    """
    Found while adding tests for 'embedding-call failure mid-batch'
    (Session 10, flagged as untested surface area) -- not something that
    was asked for directly, but the same category of gap as
    _db_write_failed: the embedding call wasn't wrapped in anything, so a
    real backend exception (OOM, a torch internal error, the model still
    mid-download) would have propagated as an unhandled exception instead
    of this project's safe-error contract. Flagged here rather than folded
    in silently.
    """
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": "server_error",
            "detail": "This document could not be processed — please try again.",
        },
    )


def _db_write_failed() -> HTTPException:
    """
    Found missing during an audit (Session 6/9): app/routers/admin.py and
    operator.py both wrap their writes in try/except DBAPIError; this file
    didn't, so a DB-level failure here would have surfaced as a raw,
    unhandled 500 instead of the project's safe-error contract.

    Uses 500 server_error, not 400 invalid_input like admin.py's DBAPIError
    handling -- a deliberate difference, not an inconsistency. admin.py's
    DBAPIError cases are specifically races against a known business
    constraint (the admin cap, username uniqueness) that really is "your
    input, at this moment, conflicts with something" -- 400 is accurate
    there. Nothing in ingest.py's writes has that shape: every constraint a
    document/chunk insert could trip (min_role_required's CHECK,
    source_type's CHECK) is already validated before this point, so a
    DBAPIError here more likely means a transient infrastructure problem
    (a dropped connection, a timeout) or an unexpected internal constraint
    violation -- telling the caller "fix your input" would be inaccurate
    either way; 500 is the honest classification.
    """
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": "server_error",
            "detail": "This document could not be saved — please try again.",
        },
    )


async def _get_document_in_tenant_or_404(db: AsyncSession, tenant_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    """Tenant-scoped lookup, not a bare db.get() -- same reasoning as
    app/routers/admin.py's _get_user_in_tenant_or_404: a caller must get the
    identical generic 404 for a document id that doesn't exist at all as for
    one that exists but belongs to another tenant, so the error shape alone
    never lets a caller fingerprint another tenant's ids."""
    result = await db.execute(select(Document).where(Document.id == document_id, Document.tenant_id == tenant_id))
    document = result.scalar_one_or_none()
    if document is None:
        raise _not_found()
    return document


async def _chunk_count(db: AsyncSession, document_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count()).select_from(DocumentVector).where(DocumentVector.document_id == document_id)
    )
    return result.scalar_one()


async def _embed_and_store(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    title: str,
    source_type: str,
    min_role_required: int | None,
    raw_text: str,
    embedding_service: EmbeddingService,
) -> Document:
    """Shared by both ingestion endpoints: chunk -> embed the batch -> write
    Document + DocumentVector rows in one transaction. Raises a safe 400 if
    there's no extractable text to chunk, before any row is written."""
    resolved_min_role = min_role_required if min_role_required is not None else Role.ADMIN.value

    chunks = chunk_text(raw_text, source_type)
    if not chunks:
        raise _bad_request("No extractable text content was found to ingest.")

    # embed_documents() is a plain blocking call under the hood (the real
    # backend's model.encode() is CPU-bound, with no async API) -- calling
    # it directly here would block this whole worker's event loop for the
    # duration, stalling every other in-flight request (login, admin, any
    # other tenant's requests) for as long as embedding takes, not just this
    # one. to_thread runs it on a worker thread instead, so the event loop
    # stays free to keep serving everything else while this finishes.
    #
    # Wrapped broadly (Exception, not a specific type): the real backend can
    # fail in ways that have nothing in common with each other -- a torch
    # OOM, a corrupted/partial model download, a library-internal bug --
    # and none of them should ever reach the caller as a raw exception.
    try:
        embeddings = await asyncio.to_thread(embedding_service.embed_documents, chunks)
    except Exception as e:
        raise _embedding_failed() from e
    strategy = chunk_strategy_label()

    document = Document(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        title=title,
        source_type=source_type,
        min_role_required=resolved_min_role,
    )
    db.add(document)

    try:
        await db.flush()  # populate document.id for the FK below

        for chunk, emb in zip(chunks, embeddings):
            db.add(DocumentVector(
                id=uuid.uuid4(),
                document_id=document.id,
                tenant_id=tenant_id,
                chunk_text=chunk,
                embedding=emb["embedding"],
                embedding_model=emb["embedding_model"],
                embedding_dims=emb["embedding_dims"],
                chunk_strategy=strategy,
            ))

        await db.commit()
    except DBAPIError:
        await db.rollback()
        raise _db_write_failed()

    await db.refresh(document)
    return document


@router.post("/code", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def ingest_code(
    body: CodeIngestRequest,
    payload: dict = Depends(require_role(*_INGEST_ROLES)),
    db: AsyncSession = Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
):
    """Ingests raw code as text (no file upload -- code is pasted/provided
    as a string, unlike PDF/markdown which arrive as files via
    POST /ingest/document)."""
    tenant_id = current_tenant_id(payload)
    document = await _embed_and_store(
        db, tenant_id, body.title, "code", body.min_role_required, body.content, embedding_service,
    )
    chunk_count = await _chunk_count(db, document.id)
    return DocumentResponse(
        id=document.id, tenant_id=document.tenant_id, title=document.title,
        source_type=document.source_type, min_role_required=document.min_role_required,
        chunk_count=chunk_count, created_at=document.created_at,
    )


@router.post("/document", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def ingest_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    min_role_required: int | None = Form(default=None),
    payload: dict = Depends(require_role(*_INGEST_ROLES)),
    db: AsyncSession = Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
):
    """PDF or markdown upload. source_type is inferred from the filename
    extension, not trusted from a client-supplied content-type header."""
    tenant_id = current_tenant_id(payload)

    filename = file.filename or ""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    source_type = _DOCUMENT_EXTENSIONS.get(ext)
    if source_type is None:
        raise _bad_request(
            "Unsupported file type. Only .pdf, .md, and .markdown files can be ingested this way "
            "— use POST /ingest/code for source code."
        )

    if min_role_required is not None and not (Role.ADMIN.value <= min_role_required <= Role.TRAINEE.value):
        raise _bad_request("min_role_required must be between 1 and 3.")

    raw_bytes = await file.read()
    max_bytes = settings.ingest_max_file_size_mb * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise _bad_request(
            f"File exceeds the {settings.ingest_max_file_size_mb}MB ingestion limit for a single upload."
        )
    if not raw_bytes:
        raise _bad_request("Uploaded file is empty.")

    if source_type == "pdf":
        # Same reasoning as the embed_documents offload below: pypdf's
        # extraction is synchronous and can take real time on a large,
        # multi-hundred-page PDF -- offloaded for the same reason, not a
        # separate decision.
        raw_text = await asyncio.to_thread(_extract_pdf_text, raw_bytes)
    else:
        raw_text = _decode_text(raw_bytes)  # a plain .decode() -- fast enough not to need this

    document_title = title or filename or "Untitled document"
    document = await _embed_and_store(
        db, tenant_id, document_title, source_type, min_role_required, raw_text, embedding_service,
    )
    chunk_count = await _chunk_count(db, document.id)
    return DocumentResponse(
        id=document.id, tenant_id=document.tenant_id, title=document.title,
        source_type=document.source_type, min_role_required=document.min_role_required,
        chunk_count=chunk_count, created_at=document.created_at,
    )


def _extract_pdf_text(raw_bytes: bytes) -> str:
    import io

    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(raw_bytes))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except PyPdfError as e:
        # Found during an audit (Session 6/9): this previously caught only
        # PdfReadError, a narrower net than pypdf's actual exception surface
        # -- PyPdfError is the common base every pypdf-specific error
        # inherits from (including encrypted-without-password via
        # FileNotDecryptedError, which is a PdfReadError subclass and so
        # was already covered either way).
        raise _bad_request("This file could not be read as a PDF.") from e
    except Exception as e:
        # Broadened further, deliberately, specifically at this one
        # untrusted-file-parsing boundary: a sufficiently malformed or
        # adversarial PDF can trip a raw Python exception somewhere in
        # pypdf's parser (a corrupted compression stream, a truncated
        # cross-reference table, etc.) that isn't one of pypdf's own
        # exception types. Caught broadly here so a bad upload becomes the
        # same safe 400 as any other malformed file, instead of an
        # unhandled 500 that could leak exception internals -- not a
        # general "catch everything" pattern used elsewhere in this
        # codebase, just at this specific boundary where the input is
        # untrusted binary data by definition.
        raise _bad_request("This file could not be read as a PDF.") from e


def _decode_text(raw_bytes: bytes) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _bad_request("This file could not be decoded as UTF-8 text.") from e


@router.patch("/document/{document_id}", response_model=DocumentResponse)
async def reclassify_document(
    document_id: uuid.UUID,
    body: DocumentPatchRequest,
    payload: dict = Depends(require_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """Admin-only: change min_role_required. Not extended to Developer even
    for a document they ingested themselves (Analysis §1)."""
    tenant_id = current_tenant_id(payload)
    document = await _get_document_in_tenant_or_404(db, tenant_id, document_id)

    document.min_role_required = body.min_role_required
    try:
        await db.commit()
    except DBAPIError:
        await db.rollback()
        raise _db_write_failed()
    await db.refresh(document)

    chunk_count = await _chunk_count(db, document.id)
    return DocumentResponse(
        id=document.id, tenant_id=document.tenant_id, title=document.title,
        source_type=document.source_type, min_role_required=document.min_role_required,
        chunk_count=chunk_count, created_at=document.created_at,
    )


@router.delete("/document/{document_id}", response_model=DocumentResponse)
async def delete_document(
    document_id: uuid.UUID,
    payload: dict = Depends(require_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    """
    Admin-only. Hard delete, unlike DELETE /admin/users' soft deactivation:
    users are soft-deleted because project_memory.created_by_user_id and
    query_log.user_id need a real row to keep resolving to ("who
    decided/asked what"). Nothing references a document the same way --
    document_vectors is the only table with a FK to it, and that FK is
    ON DELETE CASCADE (migration 0001), so removing the document removes its
    chunks with it. There's no attribution history a hard delete here would
    break.
    """
    tenant_id = current_tenant_id(payload)
    document = await _get_document_in_tenant_or_404(db, tenant_id, document_id)
    chunk_count = await _chunk_count(db, document.id)

    # Snapshot before delete -- there's no row left to refresh() afterward.
    response = DocumentResponse(
        id=document.id, tenant_id=document.tenant_id, title=document.title,
        source_type=document.source_type, min_role_required=document.min_role_required,
        chunk_count=chunk_count, created_at=document.created_at,
    )

    try:
        await db.delete(document)
        await db.commit()
    except DBAPIError:
        await db.rollback()
        raise _db_write_failed()
    return response
