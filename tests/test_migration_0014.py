"""
Migration 0014 (password lifecycle columns), on a THROWAWAY database with rows that exist BEFORE it.

Proves what matters for a migration that touches the users and operators tables:
  * every existing row gets the safe defaults: nobody is forced to change a password, no expiry,
    session_version 0, so no existing session is revoked and nobody is locked out by the upgrade;
  * the existing password hashes are untouched;
  * downgrade removes exactly the new columns and nothing else, and a second upgrade works.
Skips (does not fail) if the test database user cannot CREATE DATABASE.
"""
import uuid

import asyncpg
import pytest

from tests.test_migration_0013 import _alembic, _dsn

NEW_USER_COLUMNS = {"must_change_password", "temp_password_expires_at", "session_version"}
NEW_OPERATOR_COLUMNS = {"must_change_password", "session_version"}
T, U, O = ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222",
           "44444444-4444-4444-4444-444444444444")


@pytest.fixture
async def scratch():
    name = f"gr_mig14_{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(_dsn("postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    except asyncpg.InsufficientPrivilegeError:
        await admin.close()
        pytest.skip("test database user cannot CREATE DATABASE")
    try:
        r = _alembic(name, "upgrade", "0013")
        assert r.returncode == 0, r.stderr
        conn = await asyncpg.connect(_dsn(name))
        await conn.execute(
            f"INSERT INTO tenants (id, name) VALUES ('{T}', 't');"
            f"INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
            f"VALUES ('{U}', '{T}', 'u', 'u@mig14.local', 'existing-hash-u', 2);"
            f"INSERT INTO operators (id, username, email, hashed_password) "
            f"VALUES ('{O}', 'op', 'op@mig14.local', 'existing-hash-op');"
        )
        yield conn, name
        await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


async def _columns(conn, table):
    rows = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = $1", table)
    return {r["column_name"] for r in rows}


async def test_existing_rows_get_safe_defaults_and_keep_their_passwords(scratch):
    conn, name = scratch
    assert not (NEW_USER_COLUMNS & await _columns(conn, "users"))
    r = _alembic(name, "upgrade", "0014")
    assert r.returncode == 0, r.stderr

    u = await conn.fetchrow("SELECT * FROM users WHERE id = $1", uuid.UUID(U))
    assert u["must_change_password"] is False and u["temp_password_expires_at"] is None and u["session_version"] == 0
    assert u["hashed_password"] == "existing-hash-u"
    o = await conn.fetchrow("SELECT * FROM operators WHERE id = $1", uuid.UUID(O))
    assert o["must_change_password"] is False and o["session_version"] == 0 and o["hashed_password"] == "existing-hash-op"


async def test_new_rows_default_the_same_way_without_the_application(scratch):
    conn, name = scratch
    assert _alembic(name, "upgrade", "0014").returncode == 0
    await conn.execute(
        f"INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
        f"VALUES (gen_random_uuid(), '{T}', 'v', 'v@mig14.local', 'h', 3)")
    v = await conn.fetchrow("SELECT must_change_password, session_version FROM users WHERE username = 'v'")
    assert v["must_change_password"] is False and v["session_version"] == 0


async def test_downgrade_removes_exactly_the_new_columns_and_a_second_upgrade_works(scratch):
    conn, name = scratch
    users_before, operators_before = await _columns(conn, "users"), await _columns(conn, "operators")
    assert _alembic(name, "upgrade", "0014").returncode == 0
    assert NEW_USER_COLUMNS <= await _columns(conn, "users") and NEW_OPERATOR_COLUMNS <= await _columns(conn, "operators")
    assert _alembic(name, "downgrade", "0013").returncode == 0
    assert await _columns(conn, "users") == users_before and await _columns(conn, "operators") == operators_before
    assert await conn.fetchval("SELECT hashed_password FROM users WHERE id = $1", uuid.UUID(U)) == "existing-hash-u"
    assert _alembic(name, "upgrade", "0014").returncode == 0
