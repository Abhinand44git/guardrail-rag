"""
Project memory (Phase 5): POST /project-memory saves a decision/note tagged with an SDLC phase;
GET /project-memory lists what the caller may see. Retrieval over these entries by meaning is
POST /retrieve (app/routers/retrieve.py).

Who may do what:
  * POST -- Admin and Developer (same as ingestion: the roles that may add knowledge).
    JUDGMENT CALL, not in the source docs, which do not name a role for this endpoint: Trainees
    read but do not write, matching "Can ingest documents: no". Easy to widen.
  * GET -- every role, through the one access rule in app/rbac.py: your tenant, and either
    clearance allows it or you wrote it (ADR-0023).
  * The tenant and the role come from the caller's database row, never from the request body
    or the token's frozen claims (deps.require_live_user). project_id is a label (ADR-0004).

Default clearance for a new entry is 1 (Admin only), fail-closed, exactly like a new document
(ADR-0020); the writer may set a looser level at creation, same as ingestion allows, and always
sees their own entries whatever the level (ADR-0023).

  * PATCH /{id} and DELETE /{id} -- Admin only, like documents (ADR-0025): a role that creates
    content does not also get to reclassify or remove it. PATCH changes the clearance level and
    nothing else (changing the text would mean re-embedding: delete and save a new note). DELETE is
    a hard delete, since nothing references a memory entry. An id that does not exist and an id in
    another tenant both give the same generic 404.
"""
import asyncio
import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.deps import LiveUser, require_live_role
from app.embeddings import EmbedderUnavailable, EmbeddingService, get_embedding_service
from app.errors import db_failed, embedder_unavailable, embedding_failed, not_found
from app.models import ProjectMemory, Role
from app.phases import SdlcPhase, phases_up_to
from app.rbac import visible_memory_query
from app.schemas import MemoryCreateRequest, MemoryListResponse, MemoryPatchRequest, MemoryResponse

router = APIRouter(prefix="/project-memory", tags=["project-memory"])

_WRITE_ROLES = (Role.ADMIN.value, Role.DEVELOPER.value)
_ALL_ROLES = (Role.ADMIN.value, Role.DEVELOPER.value, Role.TRAINEE.value)


def _to_response(m: ProjectMemory) -> MemoryResponse:
    return MemoryResponse(
        id=m.id, project_id=m.project_id, phase=m.sdlc_phase, content=m.content,
        min_role_required=m.min_role_required, created_by_user_id=m.created_by_user_id,
        created_at=m.created_at,
    )


@router.post("", response_model=MemoryResponse, status_code=201)
async def create_memory(
    body: MemoryCreateRequest,
    caller: LiveUser = Depends(require_live_role(*_WRITE_ROLES)),
    db: AsyncSession = Depends(get_db),
    embedding_service: EmbeddingService = Depends(get_embedding_service),
):
    # A saved note is knowledge being stored, so it is embedded as a "document" (not a query).
    try:
        emb = (await asyncio.to_thread(embedding_service.embed_documents, [body.content]))[0]
    except EmbedderUnavailable as e:
        raise embedder_unavailable() from e
    except Exception as e:
        raise embedding_failed() from e

    entry = ProjectMemory(
        id=uuid.uuid4(),
        tenant_id=caller.tenant_id,
        project_id=body.project_id,
        sdlc_phase=body.phase,
        content=body.content,
        min_role_required=body.min_role_required if body.min_role_required is not None else Role.ADMIN.value,
        created_by_user_id=caller.user_id,
        embedding=emb["embedding"],
        embedding_model=emb["embedding_model"],
        embedding_dims=emb["embedding_dims"],
    )
    db.add(entry)
    try:
        await db.commit()
    except DBAPIError as e:
        await db.rollback()
        raise db_failed() from e
    await db.refresh(entry)
    return _to_response(entry)


@router.get("", response_model=MemoryListResponse)
async def list_memory(
    project_id: uuid.UUID | None = None,
    phase: SdlcPhase | None = None,
    include_earlier_phases: bool = True,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    caller: LiveUser = Depends(require_live_role(*_ALL_ROLES)),
    db: AsyncSession = Depends(get_db),
):
    # Starts from the access predicate; the filters below only ever narrow it.
    query = visible_memory_query(caller.tenant_id, caller.role, caller.user_id)
    if project_id is not None:
        query = query.where(ProjectMemory.project_id == project_id)  # a label filter, not security
    if phase is not None:
        query = query.where(ProjectMemory.sdlc_phase.in_(phases_up_to(phase, include_earlier_phases)))

    try:
        total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
        rows = (await db.execute(
            query.order_by(ProjectMemory.created_at.desc(), ProjectMemory.id).limit(limit).offset(offset)
        )).scalars().all()
    except DBAPIError as e:
        raise db_failed() from e

    return MemoryListResponse(items=[_to_response(m) for m in rows], total=total, limit=limit, offset=offset)


async def _entry_in_callers_tenant(db: AsyncSession, caller: LiveUser, memory_id: uuid.UUID) -> ProjectMemory:
    """Find one entry by id, through the same access rule everything else uses (tenant first), so
    there is no second hand-written tenant filter to get wrong. The callers of this are Admin-only,
    for whom the rule covers every entry in their own tenant and nothing outside it."""
    try:
        entry = (await db.execute(
            visible_memory_query(caller.tenant_id, caller.role, caller.user_id)
            .where(ProjectMemory.id == memory_id)
        )).scalar_one_or_none()
    except DBAPIError as e:
        raise db_failed() from e
    if entry is None:
        raise not_found("memory entry")
    return entry


@router.patch("/{memory_id}", response_model=MemoryResponse)
async def reclassify_memory(
    memory_id: uuid.UUID,
    body: MemoryPatchRequest,
    caller: LiveUser = Depends(require_live_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    entry = await _entry_in_callers_tenant(db, caller, memory_id)
    entry.min_role_required = body.min_role_required
    try:
        await db.commit()
    except DBAPIError as e:
        await db.rollback()
        raise db_failed() from e
    await db.refresh(entry)
    return _to_response(entry)


@router.delete("/{memory_id}", status_code=204)
async def delete_memory(
    memory_id: uuid.UUID,
    caller: LiveUser = Depends(require_live_role(Role.ADMIN.value)),
    db: AsyncSession = Depends(get_db),
):
    entry = await _entry_in_callers_tenant(db, caller, memory_id)
    await db.delete(entry)
    try:
        await db.commit()
    except DBAPIError as e:
        await db.rollback()
        raise db_failed() from e
    return Response(status_code=204)
