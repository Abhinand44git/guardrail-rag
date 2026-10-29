"""
POST /retrieve -- the core feature (ADR-0002, design 2): given a question, return the document
chunks and project-memory entries the CALLER IS ALLOWED TO SEE that are closest in meaning to it,
with their similarity scores. GuardRail never writes an answer and never calls a language model;
the caller's own model does that, from the fragments returned here.

Two lists, not one (ADR-0011 and the Phase 5 decision): a cosine score from a long document chunk
and one from a one-sentence memory note are not on a common scale, so they are returned side by
side, labelled, each ranked within itself. No score cut-off is applied: the evaluation showed
unanswerable questions scoring inside the range of correct answers, so "is this good enough" is
left to the caller.

Security order, strictly: authenticate -> take tenant and role from the DATABASE row of the
authenticated user (deps.require_live_user) -> embed the question -> run ONE query per list that
starts from the access predicate in app/rbac.py. Nothing in the request body (project_id, phase,
k, query text) can change which tenant or clearance is applied; they only narrow and order.
"""
import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import LiveUser, require_live_role
from app.embeddings import EmbedderUnavailable, EmbeddingService, get_embedding_service
from app.errors import db_failed, embedder_unavailable, embedding_failed
from app.models import Role
from app.phases import phases_up_to
from app.rbac import ranked_document_chunks_query, ranked_memory_query
from app.schemas import DocumentHit, MemoryHit, RetrieveRequest, RetrieveResponse

router = APIRouter(tags=["retrieve"])

# Every tenant role may query; what each can SEE is decided by clearance in the query itself.
_ALL_ROLES = (Role.ADMIN.value, Role.DEVELOPER.value, Role.TRAINEE.value)


@router.post("/retrieve", response_model=RetrieveResponse)
async def retrieve(
    body: RetrieveRequest,
    caller: LiveUser = Depends(require_live_role(*_ALL_ROLES)),
    db: AsyncSession = Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
):
    # The embedder call is a blocking HTTP request: off the event loop, same as ingestion.
    try:
        q = await asyncio.to_thread(embedding_service.embed_query, body.query)
    except EmbedderUnavailable as e:
        raise embedder_unavailable() from e
    except Exception as e:
        raise embedding_failed() from e

    phases = phases_up_to(body.phase, body.include_earlier_phases) if body.phase else None

    try:
        chunk_rows = (await db.execute(ranked_document_chunks_query(
            caller.tenant_id, caller.role, q["embedding"], q["embedding_model"], body.k,
        ))).all()
        memory_rows = (await db.execute(ranked_memory_query(
            caller.tenant_id, caller.role, caller.user_id, q["embedding"], q["embedding_model"], body.k,
            project_id=body.project_id, phases=phases,
        ))).all()
    except DBAPIError as e:
        raise db_failed() from e

    return RetrieveResponse(
        embedding_model=q["embedding_model"],
        documents=[
            DocumentHit(
                chunk_id=chunk.id, document_id=chunk.document_id, title=title, source_type=source_type,
                text=chunk.chunk_text, score=1.0 - distance,
            )
            for chunk, title, source_type, distance in chunk_rows
        ],
        memory=[
            MemoryHit(
                memory_id=m.id, project_id=m.project_id, phase=m.sdlc_phase, text=m.content,
                created_by_user_id=m.created_by_user_id, created_at=m.created_at, score=1.0 - distance,
            )
            for m, distance in memory_rows
        ],
    )
