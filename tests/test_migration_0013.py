"""
Migration 0013 (vectors 1536 -> 768 with no padding; project_memory clearance), tested on a
THROWAWAY database with real data -- not on the shared test database, because the point is to
run the real `alembic upgrade/downgrade` against rows that exist BEFORE the migration.

What this proves, and why it is its own test file:
  * the cut loses nothing: every stored vector comes out identical to its first 768 numbers
    (so "no re-embedding is needed" is a tested fact, not a promise);
  * a downgrade pads back with zeros and a second upgrade gives the same vectors again;
  * existing memory rows become Admin-only (fail closed);
  * the migration REFUSES, changing nothing, if any vector has real data past position 768.

Needs permission to CREATE DATABASE (the Docker image's POSTGRES_USER has it). If the test
database user lacks it, these tests skip instead of failing.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest

from app.config import settings

ROOT = Path(__file__).resolve().parent.parent
T, U, D = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
           "33333333-3333-3333-3333-333333333333")


def _dsn(db_name: str) -> str:
    parts = urlsplit(settings.database_url.replace("postgresql+asyncpg", "postgresql"))
    return urlunsplit(parts._replace(path=f"/{db_name}"))


def _alembic(db_name: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": _dsn(db_name).replace("postgresql://", "postgresql+asyncpg://", 1)}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
async def scratch():
    name = f"gr_mig_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(_dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("test database user cannot CREATE DATABASE")
    try:
        r = _alembic(name, "upgrade", "0012")
        assert r.returncode == 0, r.stderr
        conn = await asyncpg.connect(_dsn(name))
        await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        await conn.execute(
            f"INSERT INTO tenants (id, name) VALUES ('{T}', 't');"
            f"INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
            f"VALUES ('{U}', '{T}', 'u', 'u@mig.local', 'h', 2);"
            f"INSERT INTO documents (id, tenant_id, title, source_type) VALUES ('{D}', '{T}', 'd', 'code');"
        )
        yield conn, name
        await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


def _padded(seed: int) -> str:
    """768 distinct non-zero numbers followed by 768 zeros: what ingestion stored until Phase 5."""
    nums = [round(((i + 1) * seed % 997) / 997 + 0.001, 6) for i in range(768)]
    return "[" + ",".join(map(str, nums + [0] * 768)) + "]"


async def _insert_padded(conn, n=3):
    for g in range(1, n + 1):
        await conn.execute(
            "INSERT INTO document_vectors (id, document_id, tenant_id, chunk_text, embedding, embedding_model, "
            "embedding_dims, chunk_strategy) VALUES ($1, $2, $3, $4, $5::vector, 'nomic', 768, 's')",
            uuid.uuid4(), uuid.UUID(D), uuid.UUID(T), f"chunk{g}", _padded(g))
    await conn.execute(
        "INSERT INTO project_memory (id, tenant_id, project_id, sdlc_phase, content, created_by_user_id, embedding, "
        "embedding_model, embedding_dims) VALUES ($1, $2, $3, 'design', 'note', $4, $5::vector, 'nomic', 768)",
        uuid.uuid4(), uuid.UUID(T), uuid.uuid4(), uuid.UUID(U), _padded(9))


async def _snapshot(conn):
    rows = await conn.fetch("SELECT chunk_text, (embedding::real[])[1:768] AS first768 FROM document_vectors ORDER BY chunk_text")
    return {r["chunk_text"]: list(r["first768"]) for r in rows}


async def _width(conn, table):
    return await conn.fetchval(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = $1::regclass AND attname = 'embedding'", table)


async def test_upgrade_cuts_to_768_without_changing_a_single_number(scratch):
    conn, name = scratch
    await _insert_padded(conn)
    before = await _snapshot(conn)
    r = _alembic(name, "upgrade", "0013")
    assert r.returncode == 0, r.stderr
    assert await _width(conn, "document_vectors") == "vector(768)"
    assert await _width(conn, "project_memory") == "vector(768)"
    after = {r_["chunk_text"]: list(r_["v"]) for r_ in
             await conn.fetch("SELECT chunk_text, embedding::real[] AS v FROM document_vectors")}
    assert after == before and len(before) == 3
    assert all(len(v) == 768 for v in after.values())


async def test_existing_memory_rows_become_admin_only(scratch):
    conn, name = scratch
    await _insert_padded(conn)
    assert _alembic(name, "upgrade", "0013").returncode == 0
    assert await conn.fetchval("SELECT min_role_required FROM project_memory") == 1


async def test_downgrade_pads_back_with_zeros_and_a_second_upgrade_gives_identical_vectors(scratch):
    conn, name = scratch
    await _insert_padded(conn)
    before = await _snapshot(conn)
    assert _alembic(name, "upgrade", "0013").returncode == 0
    assert _alembic(name, "downgrade", "0012").returncode == 0
    assert await _width(conn, "document_vectors") == "vector(1536)"
    assert await conn.fetchval(
        "SELECT bool_and((SELECT bool_and(x = 0) FROM unnest((embedding::real[])[769:1536]) x)) FROM document_vectors")
    assert await conn.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_name='project_memory' AND column_name='min_role_required'") == 0
    assert _alembic(name, "upgrade", "0013").returncode == 0
    assert await _snapshot(conn) == before


@pytest.mark.parametrize("table", ["document_vectors", "project_memory"])
async def test_it_refuses_and_changes_nothing_if_any_vector_has_real_data_past_768(scratch, table):
    conn, name = scratch
    await _insert_padded(conn)
    wide = "[" + ",".join(str(round((i + 1) / 1536, 6)) for i in range(1536)) + "]"  # a genuinely 1536-wide vector
    if table == "document_vectors":
        await conn.execute(
            "INSERT INTO document_vectors (id, document_id, tenant_id, chunk_text, embedding, embedding_model, "
            "embedding_dims, chunk_strategy) VALUES ($1, $2, $3, 'WIDE', $4::vector, 'wide', 1536, 's')",
            uuid.uuid4(), uuid.UUID(D), uuid.UUID(T), wide)
    else:
        await conn.execute(
            "INSERT INTO project_memory (id, tenant_id, project_id, sdlc_phase, content, created_by_user_id, "
            "embedding, embedding_model, embedding_dims) VALUES ($1, $2, $3, 'design', 'WIDE', $4, $5::vector, 'wide', 1536)",
            uuid.uuid4(), uuid.UUID(T), uuid.uuid4(), uuid.UUID(U), wide)
    r = _alembic(name, "upgrade", "0013")
    assert r.returncode != 0
    assert "Cutting them would destroy data" in r.stderr
    assert await conn.fetchval("SELECT version_num FROM alembic_version") == "0012"
    assert await _width(conn, "document_vectors") == "vector(1536)"
    assert await _width(conn, "project_memory") == "vector(1536)"
    assert await conn.fetchval(
        "SELECT count(*) FROM information_schema.columns WHERE table_name='project_memory' AND column_name='min_role_required'") == 0
