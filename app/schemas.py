from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import datetime
from uuid import UUID

from app.models import Role
from app.phases import SdlcPhase


class LoginRequest(BaseModel):
    # Plain str, not EmailStr: email_validator rejects .local as a reserved
    # special-use domain, which breaks login against seeded fixture users
    # (admin@acme-corp.local etc). A bad/malformed email just won't match any
    # row and falls through to the generic "invalid email or password" —
    # strict format validation isn't needed here, and the wrong tool actively
    # breaks a real use case (see decision log, Known fixes from Phase 1).
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    plane: str


# --- Operator-plane: tenant lifecycle (Phase 3) --------------------------
# Deliberately excludes llm_provider/llm_model_name/llm_api_key_ref from every
# response below. Decision log item #5: tenant lifecycle (Operator concern)
# and tenant AI config (Admin concern, PATCH /admin/tenant-config) are kept
# as separate responsibilities -- that separation should hold at the response
# level too, not just the write-endpoint level.

class TenantCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    max_admins: int = Field(default=1, ge=1, le=5)


class TenantResponse(BaseModel):
    id: UUID
    name: str
    max_admins: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class TenantPatchRequest(BaseModel):
    is_active: bool | None = None
    max_admins: int | None = Field(default=None, ge=1, le=5)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.is_active is None and self.max_admins is None:
            raise ValueError("provide at least one of is_active or max_admins")
        return self


class AdminCreateRequest(BaseModel):
    # Deliberately no password field -- the server generates one and emails
    # it. Accepting a caller-supplied password here would mean the Operator
    # (or a network observer) learns the Admin's password, which defeats the
    # point of a one-time emailed credential.
    username: str = Field(min_length=1, max_length=64)
    # Plain str like LoginRequest.email, same .local-domain reasoning.
    email: str = Field(min_length=1, max_length=255)


class AdminCreateResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    username: str
    email: str
    role: int
    created_at: datetime
    # No password field, ever -- it exists only as a local variable during
    # the request and inside the one-time email. See app/routers/operator.py.

    model_config = {"from_attributes": True}


# --- Admin-plane: tenant user management (Phase 3.5) ----------------------
# POST/GET/PATCH/DELETE /admin/users. Same "no password field, ever" rule as
# AdminCreateResponse above -- these two response shapes are deliberately
# near-identical (this one adds is_active), not independently designed.

class UserCreateRequest(BaseModel):
    # No password field -- identical reasoning to AdminCreateRequest: the
    # server generates one and emails it, for all three roles alike.
    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=1, max_length=255)
    role: int = Field(ge=Role.ADMIN.value, le=Role.TRAINEE.value)


class UserResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    username: str
    email: str
    role: int
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserListResponse(BaseModel):
    """Pagination envelope (decision log/Analysis §2: 'pagination and role
    filtering required'). `total` is the count matching the filter, not the
    page size, so a client can compute how many pages remain."""
    items: list[UserResponse]
    total: int
    limit: int
    offset: int


class UserPatchRequest(BaseModel):
    """
    Same partial-update pattern as TenantPatchRequest: either field, or
    both, in one request. `is_active` accepts both directions (not just a
    one-way "deactivate" flag) so this same endpoint can also reactivate a
    previously-deactivated user -- the source docs describe how to
    deactivate but never separately describe a way back, and mirroring
    TenantPatchRequest's existing precedent (is_active: bool | None,
    already supports reactivating a tenant) was the smallest way to close
    that gap rather than inventing a new shape. Flagged as a judgment call,
    not a decision found in the source docs.
    """
    role: int | None = Field(default=None, ge=Role.ADMIN.value, le=Role.TRAINEE.value)
    is_active: bool | None = None
    confirm_email: str | None = Field(
        default=None,
        description=(
            "Required only when this request reactivates a currently-deactivated "
            "user (is_active: false -> true). Must match that user's stored email "
            "exactly -- a fat-finger guard against restoring access for the wrong "
            "row in a user list, not a security boundary (the acting Admin already "
            "has full visibility into every user's email via GET /admin/users)."
        ),
    )

    @model_validator(mode="after")
    def _at_least_one_field(self):
        if self.role is None and self.is_active is None:
            raise ValueError("provide at least one of role or is_active")
        return self


# --- Ingestion (Phase 4) ---------------------------------------------------
# POST /ingest/code, POST /ingest/document, PATCH/DELETE /ingest/document/{id}.

class CodeIngestRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    # Optional: defaults to Role.ADMIN.value (1) at the endpoint, not here —
    # see app/routers/ingest.py. Left optional here so the field can be
    # omitted entirely in a request body, which a hardcoded Pydantic default
    # of 1 would not distinguish from "the caller explicitly chose Admin-only".
    # In practice both end up at 1, but keeping the None case visible in the
    # schema matches how the default is described in the decision log: a
    # fallback the endpoint applies, not a value baked into every request.
    min_role_required: int | None = Field(default=None, ge=Role.ADMIN.value, le=Role.TRAINEE.value)


class DocumentResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    title: str
    source_type: str
    min_role_required: int
    chunk_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class DocumentPatchRequest(BaseModel):
    """Admin-only reclassification (Analysis §2: PATCH /ingest/document/{id}
    updates min_role_required). Single-purpose, unlike UserPatchRequest/
    TenantPatchRequest — there is exactly one mutable field on a document
    right now, so there is no partial-update ambiguity to resolve."""
    min_role_required: int = Field(ge=Role.ADMIN.value, le=Role.TRAINEE.value)


# --- Phase 5: project memory and retrieval -------------------------------
# project_id is a LABEL (ADR-0004): it narrows what a caller asks for, it never grants or
# limits access. Access is decided by the caller's tenant and clearance alone (ADR-0020).

class MemoryCreateRequest(BaseModel):
    project_id: UUID
    phase: SdlcPhase
    # 4000 characters is a number chosen here, not one from the source docs: roughly one
    # ingestion chunk (500 words). A note is one embedded row, and one long row embeds worse
    # than several short ones. Revisit with real use.
    content: str = Field(min_length=1, max_length=4000)
    # Optional: the endpoint applies the fail-closed default (1, Admin only), same pattern as
    # CodeIngestRequest.min_role_required.
    min_role_required: int | None = Field(default=None, ge=Role.ADMIN.value, le=Role.TRAINEE.value)

    @field_validator("content")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("content must not be blank")
        return v


class MemoryPatchRequest(BaseModel):
    """Reclassify only. Changing the TEXT of a note would mean re-embedding it, so the way to change
    a note is to delete it and save a new one (ADR-0025)."""
    min_role_required: int = Field(ge=Role.ADMIN.value, le=Role.TRAINEE.value)


class MemoryResponse(BaseModel):
    id: UUID
    project_id: UUID
    phase: str
    content: str
    min_role_required: int
    created_by_user_id: UUID
    created_at: datetime


class MemoryListResponse(BaseModel):
    items: list[MemoryResponse]
    total: int
    limit: int
    offset: int


class RetrieveRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    # Narrows MEMORY results only (documents have no project). A label, not a security boundary.
    project_id: UUID | None = None
    # Memory from this SDLC phase, and from every earlier phase too unless
    # include_earlier_phases is false. Omit to search memory from every phase.
    phase: SdlcPhase | None = None
    include_earlier_phases: bool = True
    # Per list: up to k document chunks AND up to k memory entries.
    k: int = Field(default=5, ge=1, le=20)

    @field_validator("query")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("query must not be blank")
        return v


class DocumentHit(BaseModel):
    chunk_id: UUID
    document_id: UUID
    title: str
    source_type: str
    text: str
    score: float  # cosine similarity, 1.0 = same direction. Not comparable with memory scores.


class MemoryHit(BaseModel):
    memory_id: UUID
    project_id: UUID
    phase: str
    text: str
    created_by_user_id: UUID
    created_at: datetime
    score: float  # cosine similarity. Not comparable with document scores.


class RetrieveResponse(BaseModel):
    embedding_model: str
    documents: list[DocumentHit]
    memory: list[MemoryHit]
