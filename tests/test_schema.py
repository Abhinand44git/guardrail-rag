"""
Schema contract tests -- Problem 3 ("docs and code drift") from the senior review.

Root cause being addressed: the same fact lives in several places (the
decision log, the Design doc, models.py, the migrations) and nothing fails
when one copy is forgotten. Seven-plus cases of that are on record (Groq
default still in the schema after the doc "fix", `project_memory` /
`documents.source_type` / `document_vectors.chunk_strategy` /
`tenants.is_active` promised but never migrated, the token blacklist).

Each test below pins ONE claim the docs make about the database, and runs
against the real Postgres + pgvector instance (same standard as test_rbac.py:
no mocks -- a CHECK constraint or trigger is only proven by the database
refusing a row). Raw SQL on purpose: the ORM would apply its own Python-side
defaults and hide what the DATABASE actually does.

Two kinds of test:
  - "contract" tests: a claim that is true today and must stay true.
  - "tripwire" tests (marked TRIPWIRE): assert something is deliberately
    ABSENT (no vector index, no query_log table, ...). They are meant to fail
    the day someone builds that thing. The failure is the prompt to write the
    ADR and update this file -- not a bug. Each says where the decision lives.

Definition of done for a schema change: migration + models.py + this file +
the decision log, with one line saying how the claim was verified.
"""
import re
import uuid
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import models  # noqa: F401 -- registers tables on Base.metadata
from app.database import Base, engine

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _vec(dims: int) -> str:
    return "[" + ",".join(["0"] * dims) + "]"


async def _columns(db, table: str) -> dict:
    rows = (
        await db.execute(
            text(
                "SELECT column_name, is_nullable, column_default "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t"
            ),
            {"t": table},
        )
    ).mappings().all()
    return {r["column_name"]: r for r in rows}


async def _insert_tenant(db, max_admins: int = 1) -> uuid.UUID:
    tid = uuid.uuid4()
    await db.execute(
        text("INSERT INTO tenants (id, name, max_admins) VALUES (:id, :n, :m)"),
        {"id": tid, "n": f"t-{tid.hex[:8]}", "m": max_admins},
    )
    return tid


async def _insert_user(db, tenant_id, role: int, active: bool = True) -> uuid.UUID:
    uid = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO users (id, tenant_id, username, email, hashed_password, role, is_active) "
            "VALUES (:id, :t, :u, :e, 'x', :r, :a)"
        ),
        {"id": uid, "t": tenant_id, "u": f"u-{uid.hex[:8]}", "e": f"{uid.hex[:8]}@schema.test",
         "r": role, "a": active},
    )
    return uid


async def _insert_document(db, tenant_id, **extra) -> uuid.UUID:
    did = uuid.uuid4()
    cols = {"id": did, "tenant_id": tenant_id, "title": "doc", "source_type": "markdown"}
    cols.update(extra)
    names = ", ".join(cols)
    values = ", ".join(f":{k}" for k in cols)
    await db.execute(text(f"INSERT INTO documents ({names}) VALUES ({values})"), cols)
    return did


async def _rejected(db, sql: str, params: dict | None = None, exc=IntegrityError):
    """The statement must be refused by the DATABASE. Savepoint keeps the
    outer test transaction usable afterwards."""
    with pytest.raises(exc):
        async with db.begin_nested():
            await db.execute(text(sql), params or {})


# --------------------------------------------------------------------------
# 1. The set of tables, and the migration chain
# --------------------------------------------------------------------------
EXPECTED_TABLES = {
    "alembic_version",
    "operators",
    "tenants",
    "users",
    "documents",
    "document_vectors",
    "project_memory",
    "token_blacklist",
}


async def test_table_set_matches_the_documented_schema(db_session):
    """TRIPWIRE. The exact set of tables. Deliberately ABSENT today, each
    decided somewhere on record:
      - query_log, user_llm_config  -> deferred with the hosted-generation
                                       design ("design 1"), not dropped.
      - projects, project_members   -> decided NOT to build (handoff section 4,
                                       decision 4): project_id is a client-
                                       supplied label, never a security boundary.
      - roles, document_metadata    -> Design-doc names that the shipped schema
                                       deviates from (int role column; `documents`).
    Adding any of these is a decision: write the ADR, then update this set."""
    rows = (
        await db_session.execute(
            text("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        )
    ).scalars().all()
    assert set(rows) == EXPECTED_TABLES


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_migration_history_is_one_linear_chain():
    """One head, no branches, no gaps in the numbering (0001, 0002, ...)."""
    script = _script_dir()
    assert len(script.get_heads()) == 1, "migration history has branched"
    revs = [r.revision for r in script.walk_revisions()][::-1]  # base -> head
    assert revs == [f"{i:04d}" for i in range(1, len(revs) + 1)]


async def test_database_is_at_the_migration_head(db_session):
    """The database these tests ran against is fully migrated -- otherwise
    every other assertion in this file is about a stale schema."""
    current = (await db_session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one()
    assert current == _script_dir().get_current_head()


# --------------------------------------------------------------------------
# 2. models.py vs the migrations (the drift detector)
# --------------------------------------------------------------------------
# What Alembic's autogenerate comparison finds today between models.py and the
# migrated database. The app runs on the MIGRATIONS, so none of this changes
# behaviour -- but it is exactly the "same fact in two places" problem, so it
# is written down and bounded rather than ignored. A NEW difference fails the
# test; so does an entry here that stops being true (fix models.py, delete the
# line). That keeps this list honest in both directions.
KNOWN_MODEL_VS_MIGRATION_DRIFT = {
    # (a) The database has indexes that models.py never declares.
    "remove_index:ix_document_vectors_document_id",
    "remove_index:ix_document_vectors_embedding_model",
    "remove_index:ix_document_vectors_tenant_id",
    "remove_index:ix_documents_tenant_id",
    "remove_index:ix_project_memory_tenant_project",
    "remove_index:ix_token_blacklist_expires_at",
    "remove_index:ix_users_email",
    "remove_index:ix_users_tenant_id",
    # (b) The database has server-side defaults that models.py only sets in
    #     Python. Raw SQL inserts get the DB default; ORM inserts get the
    #     Python one. Same values today.
    "modify_default:documents.min_role_required",
    "modify_default:tenants.max_admins",
    "modify_default:tenants.is_active",
    "modify_default:users.is_active",
    # (c) created_at / blacklisted_at are NULLABLE in the database (they have a
    #     now() default, but nothing forbids an explicit NULL) while models.py
    #     declares them non-null.
    "modify_nullable:document_vectors.created_at",
    "modify_nullable:documents.created_at",
    "modify_nullable:operators.created_at",
    "modify_nullable:project_memory.created_at",
    "modify_nullable:tenants.created_at",
    "modify_nullable:token_blacklist.blacklisted_at",
    "modify_nullable:users.created_at",
    # (d) Cosmetic: TEXT in the database, unbounded String in models.py; and a
    #     unique INDEX (migration 0003) where models.py says unique=True.
    "modify_type:document_vectors.chunk_text",
    "modify_type:project_memory.content",
    "add_constraint:users(email)",
}


def _diff_keys(diffs) -> set[str]:
    keys: set[str] = set()
    for d in diffs:
        for item in (d if isinstance(d, list) else [d]):
            op = item[0]
            if op in ("modify_type", "modify_nullable", "modify_default"):
                keys.add(f"{op}:{item[2]}.{item[3]}")
            elif op in ("add_index", "remove_index"):
                keys.add(f"{op}:{item[1].name}")
            elif op in ("add_table", "remove_table"):
                keys.add(f"{op}:{item[1].name}")
            elif op in ("add_column", "remove_column"):
                keys.add(f"{op}:{item[2]}.{item[3].name}")
            elif op in ("add_constraint", "remove_constraint"):
                c = item[1]
                cols = ",".join(sorted(col.name for col in c.columns))
                keys.add(f"{op}:{c.table.name}({cols})" if not c.name else f"{op}:{c.name}")
            else:
                keys.add(f"{op}:{item[1:]!r}")
    return keys


def _compare(sync_conn):
    ctx = MigrationContext.configure(
        sync_conn, opts={"compare_type": True, "compare_server_default": True}
    )
    return compare_metadata(ctx, Base.metadata)


async def test_models_and_migrations_agree_except_for_the_known_list():
    async with engine.connect() as conn:
        diffs = await conn.run_sync(_compare)
    found = _diff_keys(diffs)
    assert found - KNOWN_MODEL_VS_MIGRATION_DRIFT == set(), (
        "NEW drift between app/models.py and the migrations -- fix one of them "
        "(a table/column/constraint in only one place is how past bugs started)"
    )
    assert KNOWN_MODEL_VS_MIGRATION_DRIFT - found == set(), (
        "A known-drift entry is no longer true (someone fixed models.py?) -- "
        "delete it from KNOWN_MODEL_VS_MIGRATION_DRIFT so the list stays honest"
    )


# --------------------------------------------------------------------------
# 3. tenants
# --------------------------------------------------------------------------
@pytest.mark.parametrize("col", ["llm_provider", "llm_model_name", "llm_api_key_ref"])
async def test_tenant_llm_columns_are_nullable_with_no_default(db_session, col):
    """Migration 0004 / 'Major correction': GuardRail owns no generation model,
    so there is NO default provider. The old schema defaulted to 'groq'."""
    c = (await _columns(db_session, "tenants"))[col]
    assert c["is_nullable"] == "YES"
    assert c["column_default"] is None


async def test_new_tenant_has_no_model_configured(db_session):
    tid = await _insert_tenant(db_session)
    row = (
        await db_session.execute(
            text("SELECT llm_provider, llm_model_name, llm_api_key_ref FROM tenants WHERE id = :i"),
            {"i": tid},
        )
    ).one()
    assert tuple(row) == (None, None, None)


async def test_tenant_defaults(db_session):
    tid = uuid.uuid4()
    await db_session.execute(text("INSERT INTO tenants (id, name) VALUES (:i, 'x')"), {"i": tid})
    row = (
        await db_session.execute(
            text("SELECT max_admins, is_active, created_by_operator_id FROM tenants WHERE id = :i"),
            {"i": tid},
        )
    ).one()
    assert tuple(row) == (1, True, None)  # max_admins default 1; active; no operator recorded


@pytest.mark.parametrize("ok", [1, 5])
async def test_max_admins_accepts_the_documented_range(db_session, ok):
    await _insert_tenant(db_session, max_admins=ok)


@pytest.mark.parametrize("bad", [0, 6, -1])
async def test_max_admins_rejects_values_outside_1_to_5(db_session, bad):
    await _rejected(db_session, "INSERT INTO tenants (id, name, max_admins) VALUES (:i, 'x', :m)",
                    {"i": uuid.uuid4(), "m": bad})


async def test_tenant_created_by_operator_is_a_real_foreign_key(db_session):
    await _rejected(
        db_session,
        "INSERT INTO tenants (id, name, created_by_operator_id) VALUES (:i, 'x', :o)",
        {"i": uuid.uuid4(), "o": uuid.uuid4()},
    )


# --------------------------------------------------------------------------
# 4. operators and users (the two identity planes)
# --------------------------------------------------------------------------
async def test_operators_table_has_no_tenant_or_role_columns(db_session):
    """Structural isolation (Session 1): operators are global. There is no
    tenant_id for a bug to fill in, and no role column to escalate."""
    cols = set(await _columns(db_session, "operators"))
    assert "tenant_id" not in cols
    assert "role" not in cols


async def test_operator_email_and_username_are_unique(db_session):
    async def op(u, e):
        await db_session.execute(
            text("INSERT INTO operators (id, username, email, hashed_password) VALUES (:i, :u, :e, 'x')"),
            {"i": uuid.uuid4(), "u": u, "e": e},
        )

    await op("op-a", "a@op.test")
    await _rejected(db_session, "INSERT INTO operators (id, username, email, hashed_password) "
                                "VALUES (:i, 'op-b', 'a@op.test', 'x')", {"i": uuid.uuid4()})
    await _rejected(db_session, "INSERT INTO operators (id, username, email, hashed_password) "
                                "VALUES (:i, 'op-a', 'b@op.test', 'x')", {"i": uuid.uuid4()})


async def test_username_is_unique_per_tenant_not_globally(db_session):
    """Migration 0006: two companies may both have an admin called 'admin';
    one company may not have two."""
    t1, t2 = await _insert_tenant(db_session, 5), await _insert_tenant(db_session, 5)

    async def user(tenant, username, email):
        await db_session.execute(
            text("INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
                 "VALUES (:i, :t, :u, :e, 'x', 2)"),
            {"i": uuid.uuid4(), "t": tenant, "u": username, "e": email},
        )

    await user(t1, "admin", "a@t1.test")
    await user(t2, "admin", "a@t2.test")  # same username, other tenant: allowed
    await _rejected(
        db_session,
        "INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
        "VALUES (:i, :t, 'admin', 'other@t1.test', 'x', 2)",
        {"i": uuid.uuid4(), "t": t1},
    )


async def test_email_is_unique_across_all_tenants(db_session):
    """Migration 0003: /auth/login resolves a user by email alone, so one
    email must never match two rows -- even in different tenants."""
    t1, t2 = await _insert_tenant(db_session), await _insert_tenant(db_session)
    await db_session.execute(
        text("INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
             "VALUES (:i, :t, 'a', 'same@x.test', 'x', 2)"),
        {"i": uuid.uuid4(), "t": t1},
    )
    await _rejected(
        db_session,
        "INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
        "VALUES (:i, :t, 'b', 'same@x.test', 'x', 2)",
        {"i": uuid.uuid4(), "t": t2},
    )


@pytest.mark.parametrize("bad_role", [0, 4, -1])
async def test_user_role_must_be_1_2_or_3(db_session, bad_role):
    t = await _insert_tenant(db_session)
    await _rejected(
        db_session,
        "INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
        "VALUES (:i, :t, 'u', 'u@x.test', 'x', :r)",
        {"i": uuid.uuid4(), "t": t, "r": bad_role},
    )


async def test_user_is_active_defaults_to_true(db_session):
    t = await _insert_tenant(db_session)
    uid = uuid.uuid4()
    await db_session.execute(
        text("INSERT INTO users (id, tenant_id, username, email, hashed_password, role) "
             "VALUES (:i, :t, 'u', 'u@x.test', 'x', 3)"),
        {"i": uid, "t": t},
    )
    assert (await db_session.execute(text("SELECT is_active FROM users WHERE id = :i"), {"i": uid})).scalar_one() is True


# --------------------------------------------------------------------------
# 5. trg_enforce_max_admins -- the database-level admin cap
# --------------------------------------------------------------------------
async def test_trigger_fires_on_insert_and_on_update_of_role_and_is_active(db_session):
    """Migration 0008. Before it, the trigger was INSERT-only and promotion /
    reactivation silently bypassed the cap."""
    definition = (
        await db_session.execute(
            text("SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                 "WHERE tgname = 'trg_enforce_max_admins' AND NOT tgisinternal")
        )
    ).scalar_one()
    assert "BEFORE INSERT OR UPDATE OF role, is_active ON public.users" in definition
    assert "FOR EACH ROW" in definition


async def test_trigger_blocks_inserting_an_admin_at_the_cap(db_session):
    t = await _insert_tenant(db_session, max_admins=1)
    await _insert_user(db_session, t, role=1)
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():  # savepoint inside raises(), so the error escapes it cleanly
            await _insert_user(db_session, t, role=1)


async def test_trigger_blocks_promotion_to_admin_at_the_cap(db_session):
    t = await _insert_tenant(db_session, max_admins=1)
    await _insert_user(db_session, t, role=1)
    dev = await _insert_user(db_session, t, role=2)
    await _rejected(db_session, "UPDATE users SET role = 1 WHERE id = :i", {"i": dev})


async def test_deactivating_an_admin_frees_the_slot(db_session):
    t = await _insert_tenant(db_session, max_admins=1)
    admin = await _insert_user(db_session, t, role=1)
    dev = await _insert_user(db_session, t, role=2)
    await db_session.execute(text("UPDATE users SET is_active = false WHERE id = :i"), {"i": admin})
    await db_session.execute(text("UPDATE users SET role = 1 WHERE id = :i"), {"i": dev})  # now allowed


async def test_trigger_blocks_reactivating_an_admin_over_the_cap(db_session):
    t = await _insert_tenant(db_session, max_admins=1)
    old = await _insert_user(db_session, t, role=1)
    await db_session.execute(text("UPDATE users SET is_active = false WHERE id = :i"), {"i": old})
    await _insert_user(db_session, t, role=1)  # the replacement Admin
    await _rejected(db_session, "UPDATE users SET is_active = true WHERE id = :i", {"i": old})


async def test_resending_role_and_is_active_on_an_active_admin_is_not_blocked(db_session):
    """A no-op write on a row that is already an active Admin must not trip
    the cap (the row is already counted)."""
    t = await _insert_tenant(db_session, max_admins=1)
    admin = await _insert_user(db_session, t, role=1)
    await db_session.execute(
        text("UPDATE users SET role = 1, is_active = true WHERE id = :i"), {"i": admin}
    )


async def test_lowering_the_cap_grandfathers_existing_admins(db_session):
    """Session 3: lowering max_admins removes nobody; it only blocks new ones."""
    t = await _insert_tenant(db_session, max_admins=2)
    a1, a2 = await _insert_user(db_session, t, 1), await _insert_user(db_session, t, 1)
    await db_session.execute(text("UPDATE tenants SET max_admins = 1 WHERE id = :i"), {"i": t})
    remaining = (
        await db_session.execute(
            text("SELECT count(*) FROM users WHERE tenant_id = :t AND role = 1 AND is_active"), {"t": t}
        )
    ).scalar_one()
    assert remaining == 2
    await db_session.execute(  # touching a grandfathered Admin is still allowed
        text("UPDATE users SET role = 1, is_active = true WHERE id IN (:a, :b)"), {"a": a1, "b": a2}
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await _insert_user(db_session, t, 1)  # but a third is not


async def test_the_cap_is_counted_per_tenant(db_session):
    t1, t2 = await _insert_tenant(db_session, 1), await _insert_tenant(db_session, 1)
    await _insert_user(db_session, t1, 1)
    await _insert_user(db_session, t2, 1)  # t1 being full must not affect t2


async def test_demoting_or_deactivating_is_never_blocked_even_over_cap(db_session):
    t = await _insert_tenant(db_session, max_admins=2)
    a1, a2 = await _insert_user(db_session, t, 1), await _insert_user(db_session, t, 1)
    await db_session.execute(text("UPDATE tenants SET max_admins = 1 WHERE id = :i"), {"i": t})
    await db_session.execute(text("UPDATE users SET role = 2 WHERE id = :i"), {"i": a1})
    await db_session.execute(text("UPDATE users SET is_active = false WHERE id = :i"), {"i": a2})


# --------------------------------------------------------------------------
# 6. documents and document_vectors (what the RBAC query reads)
# --------------------------------------------------------------------------
async def test_document_min_role_defaults_to_admin_only_in_the_database(db_session):
    """Session 5 decision #3, migration 0010: FAIL CLOSED. A document inserted
    without saying who may read it is Admin-only (1), not Trainee-visible (3).
    Checked with raw SQL so the ORM's own Python default can't mask it."""
    t = await _insert_tenant(db_session)
    did = await _insert_document(db_session, t)
    got = (await db_session.execute(text("SELECT min_role_required FROM documents WHERE id = :i"), {"i": did})).scalar_one()
    assert got == 1


@pytest.mark.parametrize("bad", [0, 4])
async def test_document_min_role_range(db_session, bad):
    t = await _insert_tenant(db_session)
    await _rejected(
        db_session,
        "INSERT INTO documents (id, tenant_id, title, source_type, min_role_required) "
        "VALUES (:i, :t, 'd', 'pdf', :r)",
        {"i": uuid.uuid4(), "t": t, "r": bad},
    )


async def test_document_source_type_is_required_and_limited(db_session):
    cols = await _columns(db_session, "documents")
    assert cols["source_type"]["is_nullable"] == "NO"
    assert cols["source_type"]["column_default"] is None  # callers must say what it is
    t = await _insert_tenant(db_session)
    for ok in ("code", "pdf", "markdown"):
        await _insert_document(db_session, t, source_type=ok)
    await _rejected(
        db_session,
        "INSERT INTO documents (id, tenant_id, title, source_type) VALUES (:i, :t, 'd', 'docx')",
        {"i": uuid.uuid4(), "t": t},
    )


async def test_chunk_columns_are_required(db_session):
    cols = await _columns(db_session, "document_vectors")
    for name in ("tenant_id", "chunk_text", "embedding", "embedding_model", "embedding_dims", "chunk_strategy"):
        assert cols[name]["is_nullable"] == "NO", name
    # chunk_strategy has no default on purpose: every row records how it was made.
    assert cols["chunk_strategy"]["column_default"] is None


async def test_embedding_columns_are_vector_768(db_session):
    """ADR-0019 / migration 0013: exactly nomic's native width, no padding."""
    for table in ("document_vectors", "project_memory"):
        got = (
            await db_session.execute(
                text("SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                     "WHERE attrelid = CAST(:t AS regclass) AND attname = 'embedding'"),
                {"t": table},
            )
        ).scalar_one()
        assert got == "vector(768)", table


async def test_a_vector_of_the_wrong_width_is_refused_by_the_database(db_session):
    """The database is the last line of defence for ADR-0019: the old padded 1536-wide vector,
    and any other width, is rejected. (app/embeddings.py refuses first, with a clearer message.)"""
    t = await _insert_tenant(db_session)
    d = await _insert_document(db_session, t)
    sql = ("INSERT INTO document_vectors (id, document_id, tenant_id, chunk_text, embedding, "
           "embedding_model, embedding_dims, chunk_strategy) "
           "VALUES (:i, :d, :t, 'c', CAST(:v AS vector), 'm', :n, 's')")
    base = {"d": d, "t": t}
    for wrong in (1536, 512, 8):
        await _rejected(db_session, sql, {**base, "i": uuid.uuid4(), "v": _vec(wrong), "n": wrong}, exc=DBAPIError)
    await db_session.execute(text(sql), {**base, "i": uuid.uuid4(), "v": _vec(768), "n": 768})  # exact width: ok


async def test_no_vector_index_exists_yet(db_session):
    """TRIPWIRE. The Design doc specified ivfflat; no migration ever created
    one, so every search is an exact scan (review item 13). Fine at demo size.
    When an index is added: write the ADR (which type, which operator class,
    recall impact -- an approximate index changes results) and update this."""
    rows = (
        await db_session.execute(
            text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                 "AND (indexdef ILIKE '%ivfflat%' OR indexdef ILIKE '%hnsw%')")
        )
    ).scalars().all()
    assert rows == []


async def test_deleting_a_document_deletes_its_chunks(db_session):
    t = await _insert_tenant(db_session)
    d = await _insert_document(db_session, t)
    await db_session.execute(
        text("INSERT INTO document_vectors (id, document_id, tenant_id, chunk_text, embedding, "
             "embedding_model, embedding_dims, chunk_strategy) "
             "VALUES (:i, :d, :t, 'c', CAST(:v AS vector), 'm', 768, 's')"),
        {"i": uuid.uuid4(), "d": d, "t": t, "v": _vec(768)},
    )
    await db_session.execute(text("DELETE FROM documents WHERE id = :d"), {"d": d})
    left = (await db_session.execute(text("SELECT count(*) FROM document_vectors WHERE document_id = :d"), {"d": d})).scalar_one()
    assert left == 0


# --------------------------------------------------------------------------
# 7. project_memory
# --------------------------------------------------------------------------
async def _memory_insert_sql(phase: str) -> str:
    return (
        "INSERT INTO project_memory (id, tenant_id, project_id, sdlc_phase, content, created_by_user_id, "
        "embedding, embedding_model, embedding_dims) "
        f"VALUES (:i, :t, :p, '{phase}', 'note', :u, CAST(:v AS vector), 'm', 768)"
    )


@pytest.mark.parametrize("phase", ["planning", "analysis", "design", "development", "testing", "deployment"])
async def test_project_memory_accepts_each_sdlc_phase(db_session, phase):
    t = await _insert_tenant(db_session)
    u = await _insert_user(db_session, t, 2)
    await db_session.execute(
        text(await _memory_insert_sql(phase)),
        {"i": uuid.uuid4(), "t": t, "p": uuid.uuid4(), "u": u, "v": _vec(768)},
    )


async def test_project_memory_rejects_an_unknown_phase(db_session):
    t = await _insert_tenant(db_session)
    u = await _insert_user(db_session, t, 2)
    await _rejected(
        db_session, await _memory_insert_sql("maintenance"),
        {"i": uuid.uuid4(), "t": t, "p": uuid.uuid4(), "u": u, "v": _vec(768)},
    )


async def test_project_memory_shape(db_session):
    """Pins the Phase 5 memory decisions (ADR-0020, ADR-0004):
      - min_role_required EXISTS, NOT NULL, default 1 (fail closed: Admin only), and limited to
        1..3 -- the same clearance rule documents use, so one predicate in app/rbac.py covers both.
      - still NO chunk_strategy: one note = one embedded row.
    And project_id is a bare label: no foreign key, no `projects` table. It must never be
    described as a security boundary."""
    cols = await _columns(db_session, "project_memory")
    assert cols["min_role_required"]["is_nullable"] == "NO"
    assert str(cols["min_role_required"]["column_default"]).strip("()") == "1"
    assert "chunk_strategy" not in cols
    fks = (
        await db_session.execute(
            text("SELECT a.attname FROM pg_constraint c "
                 "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                 "WHERE c.conrelid = 'project_memory'::regclass AND c.contype = 'f'")
        )
    ).scalars().all()
    assert "project_id" not in fks


async def test_a_memory_row_defaults_to_admin_only_in_the_database(db_session):
    t = await _insert_tenant(db_session)
    u = await _insert_user(db_session, t, 2)
    mid = uuid.uuid4()
    await db_session.execute(
        text(await _memory_insert_sql("design")),
        {"i": mid, "t": t, "p": uuid.uuid4(), "u": u, "v": _vec(768)},
    )
    got = (await db_session.execute(text("SELECT min_role_required FROM project_memory WHERE id = :i"), {"i": mid})).scalar_one()
    assert got == 1


@pytest.mark.parametrize("bad", [0, 4, -1])
async def test_memory_min_role_range(db_session, bad):
    t = await _insert_tenant(db_session)
    u = await _insert_user(db_session, t, 2)
    await _rejected(
        db_session,
        "INSERT INTO project_memory (id, tenant_id, project_id, sdlc_phase, content, created_by_user_id, "
        "min_role_required, embedding, embedding_model, embedding_dims) "
        f"VALUES (:i, :t, :p, 'design', 'n', :u, {bad}, CAST(:v AS vector), 'm', 768)",
        {"i": uuid.uuid4(), "t": t, "p": uuid.uuid4(), "u": u, "v": _vec(768)},
    )


def test_the_phase_order_in_code_matches_the_database_constraint():
    """app/phases.py (the ORDER) and ck_project_memory_sdlc_phase (the allowed NAMES) are two
    lists of one fact. Adding a phase to one and not the other must fail here."""
    from app.models import ProjectMemory
    from app.phases import SDLC_PHASES

    check = next(c for c in ProjectMemory.__table__.constraints if c.name == "ck_project_memory_sdlc_phase")
    in_constraint = set(re.findall(r"'([a-z]+)'", str(check.sqltext)))
    assert in_constraint == set(SDLC_PHASES)


async def test_a_user_who_wrote_memory_cannot_be_hard_deleted(db_session):
    """Why users are soft-deactivated (Session 4): project_memory keeps
    created_by_user_id pointing at a real row, so 'who decided what' survives.
    The FK has NO cascade -- the database itself refuses the hard delete."""
    t = await _insert_tenant(db_session)
    u = await _insert_user(db_session, t, 2)
    await db_session.execute(
        text(await _memory_insert_sql("design")),
        {"i": uuid.uuid4(), "t": t, "p": uuid.uuid4(), "u": u, "v": _vec(768)},
    )
    await _rejected(db_session, "DELETE FROM users WHERE id = :u", {"u": u})


# --------------------------------------------------------------------------
# 8. token_blacklist
# --------------------------------------------------------------------------
async def test_token_blacklist_shape(db_session):
    """Session 7 / migration 0012: jti alone is the primary key; tenant_id is
    nullable because operator tokens carry none; expires_at is required."""
    pk = (
        await db_session.execute(
            text("SELECT a.attname FROM pg_index i "
                 "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey) "
                 "WHERE i.indrelid = 'token_blacklist'::regclass AND i.indisprimary")
        )
    ).scalars().all()
    assert pk == ["token_jti"]
    cols = await _columns(db_session, "token_blacklist")
    assert cols["tenant_id"]["is_nullable"] == "YES"
    assert cols["expires_at"]["is_nullable"] == "NO"
    assert cols["blacklisted_at"]["column_default"] is not None  # now()


async def test_blacklisting_an_operator_token_needs_no_tenant(db_session):
    await db_session.execute(
        text("INSERT INTO token_blacklist (token_jti, tenant_id, expires_at) "
             "VALUES (:j, NULL, now() + interval '1 hour')"),
        {"j": str(uuid.uuid4())},
    )


async def test_token_blacklist_expires_at_is_indexed_for_a_future_purge_job(db_session):
    """Review item 11: expired rows are never purged (no cleanup job exists).
    The index that a purge would use is already there."""
    idx = (
        await db_session.execute(
            text("SELECT indexdef FROM pg_indexes WHERE tablename = 'token_blacklist' "
                 "AND indexname = 'ix_token_blacklist_expires_at'")
        )
    ).scalar_one_or_none()
    assert idx is not None and "(expires_at)" in idx


# --------------------------------------------------------------------------
# 9. tenant deletion removes everything it owns
# --------------------------------------------------------------------------
async def test_deleting_a_tenant_cascades_to_everything_it_owns(db_session):
    t = await _insert_tenant(db_session)
    u = await _insert_user(db_session, t, 2)
    d = await _insert_document(db_session, t)
    await db_session.execute(
        text("INSERT INTO document_vectors (id, document_id, tenant_id, chunk_text, embedding, "
             "embedding_model, embedding_dims, chunk_strategy) "
             "VALUES (:i, :d, :t, 'c', CAST(:v AS vector), 'm', 768, 's')"),
        {"i": uuid.uuid4(), "d": d, "t": t, "v": _vec(768)},
    )
    await db_session.execute(
        text(await _memory_insert_sql("design")),
        {"i": uuid.uuid4(), "t": t, "p": uuid.uuid4(), "u": u, "v": _vec(768)},
    )
    await db_session.execute(
        text("INSERT INTO token_blacklist (token_jti, tenant_id, expires_at) "
             "VALUES (:j, :t, now() + interval '1 hour')"),
        {"j": str(uuid.uuid4()), "t": t},
    )
    await db_session.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": t})
    for table in ("users", "documents", "document_vectors", "project_memory", "token_blacklist"):
        n = (await db_session.execute(text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"), {"t": t})).scalar_one()
        assert n == 0, table
