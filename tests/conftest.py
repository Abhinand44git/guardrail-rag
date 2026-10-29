"""
Shared fixtures for the Phase 2 security-core test suite.

db_session gives each test its own transaction, wrapped in a SAVEPOINT, that
is rolled back at teardown -- so tests can freely INSERT fixture rows
(tenants, users, documents, document_vectors) without needing manual cleanup
and without tests leaking data into each other. This runs against the real
Postgres + pgvector instance configured via DATABASE_URL, not a mock -- the
RBAC query is a raw SQL comparison, so a real DB is what actually proves it.
"""
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine_per_test():
    """
    pytest-asyncio gives each test function its own event loop by default,
    but app.database.engine's connection pool is a process-lifetime
    singleton. A pooled asyncpg connection opened under test A's loop and
    then handed to test B's (different) loop surfaces as
    'InterfaceError: another operation is in progress'. Disposing the pool
    after every test forces the next test to open fresh connections on its
    own loop -- this is test-harness plumbing, not something app code needs
    to care about (the real app runs one event loop for its whole process).
    """
    yield
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session():
    async with engine.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()
