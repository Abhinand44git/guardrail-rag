"""
The API's public surface, pinned (Problem 3: docs and code drifting apart).

`/auth/refresh` was written into the Analysis doc but never built, in any phase; this file makes
that kind of gap impossible to miss in either direction. Adding or removing an endpoint fails
the route-table test until the list below -- and the docs -- are updated on purpose.
"""
import uuid

from app.main import app as api_app
from app.models import Role
from tests.test_ingest_endpoints import _cleanup, _make_client, _make_tenant, _make_user, _token

# FastAPI's own documentation routes are not part of GuardRail's API.
_FRAMEWORK_ROUTES = {"/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}

EXPECTED_ROUTES = {
    ("GET", "/health"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("POST", "/operator/login"),
    ("POST", "/operator/logout"),
    ("POST", "/operator/tenants"),
    ("PATCH", "/operator/tenants/{tenant_id}"),
    ("POST", "/operator/tenants/{tenant_id}/admin"),
    ("POST", "/admin/users"),
    ("GET", "/admin/users"),
    ("PATCH", "/admin/users/{user_id}"),
    ("DELETE", "/admin/users/{user_id}"),
    ("POST", "/ingest/code"),
    ("POST", "/ingest/document"),
    ("PATCH", "/ingest/document/{document_id}"),
    ("DELETE", "/ingest/document/{document_id}"),
    ("POST", "/project-memory"),
    ("GET", "/project-memory"),
    ("POST", "/retrieve"),
}


def _actual_routes() -> set[tuple[str, str]]:
    return {
        (method, route.path)
        for route in api_app.routes
        if getattr(route, "methods", None) and route.path not in _FRAMEWORK_ROUTES
        for method in route.methods - {"HEAD", "OPTIONS"}
    }


def test_the_route_table_is_exactly_the_documented_surface():
    actual = _actual_routes()
    assert actual - EXPECTED_ROUTES == set(), "an endpoint exists that this list (and the docs) do not mention"
    assert EXPECTED_ROUTES - actual == set(), "an endpoint is listed here (and in the docs) but does not exist"


def test_there_is_no_refresh_endpoint():
    """TRIPWIRE (ADR-0016): tokens last jwt_expire_minutes and then the user signs in again.
    Building refresh tokens is a security-critical change of its own: write the ADR first."""
    assert not [path for _, path in _actual_routes() if "refresh" in path]


async def test_plus_addressed_emails_let_one_person_hold_separate_accounts_in_different_tenants():
    """ADR-0017: one email = one account in one tenant, by design. A consultant working for two
    clients uses two addresses, e.g. name+clienta@... and name+clientb@..., which most mail
    providers deliver to the same inbox. This proves the system accepts exactly that."""
    t1, t2 = await _make_tenant(), await _make_tenant()
    a1, a2 = await _make_user(t1, Role.ADMIN.value), await _make_user(t2, Role.ADMIN.value)
    tag = uuid.uuid4().hex[:8]
    try:
        async with await _make_client() as client:
            for admin, tenant, who in ((a1, t1, "clienta"), (a2, t2, "clientb")):
                r = await client.post(
                    "/admin/users",
                    json={"username": "consultant", "email": f"pat+{who}-{tag}@consulting-test.local",
                          "role": Role.DEVELOPER.value},
                    headers={"Authorization": f"Bearer {_token(admin, tenant, Role.ADMIN.value)}"},
                )
                assert r.status_code == 201, r.text
    finally:
        await _cleanup(t1, t2)
