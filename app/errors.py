"""
Safe-error constructors shared by the Phase 5 routers (retrieve.py, project_memory.py).

Same shapes and same wording policy as the ones in app/routers/ingest.py (which keeps its own
private copies -- left untouched this phase so Phase 4 code did not change; consolidating the
two is a small follow-up, not done silently here). Contract (Design doc section 6): a generic,
tenant-agnostic message; never a tenant id, role number, table or column name, raw exception
text or raw embedder text.
"""
from fastapi import HTTPException, status


def embedder_unavailable() -> HTTPException:
    """The embedder could not be reached or is not ready. Transient: retrying is sensible."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "embedder_unavailable",
            "detail": "The embedding service is temporarily unavailable — please try again shortly.",
        },
    )


def embedding_failed() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "server_error", "detail": "This request could not be processed — please try again."},
    )


def db_failed() -> HTTPException:
    """A database error on a read or write that should have worked: infrastructure, not input."""
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "server_error", "detail": "This request could not be completed — please try again."},
    )


def not_found(what: str = "resource") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "detail": f"{what} not found"},
    )
