"""trg_enforce_max_admins: block admin creation past tenants.max_admins

Fires on INSERT only, by design — lowering max_admins on an existing tenant
must NOT retroactively demote/remove admins over the new cap; it should only
block new admin creation until the count drops back under the cap naturally
(see decision log: 'Operator can now adjust max_admins').

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-09

"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION enforce_max_admins() RETURNS TRIGGER AS $$
DECLARE
    admin_count INTEGER;
    cap INTEGER;
BEGIN
    IF NEW.role <> 1 THEN
        RETURN NEW;
    END IF;

    SELECT max_admins INTO cap FROM tenants WHERE id = NEW.tenant_id;

    SELECT COUNT(*) INTO admin_count
    FROM users
    WHERE tenant_id = NEW.tenant_id AND role = 1;

    IF admin_count >= cap THEN
        RAISE EXCEPTION 'tenant % already has % admin(s), max_admins is %',
            NEW.tenant_id, admin_count, cap
            USING ERRCODE = '23514'; -- check_violation
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_TRIGGER_SQL = """
CREATE TRIGGER trg_enforce_max_admins
    BEFORE INSERT ON users
    FOR EACH ROW
    EXECUTE FUNCTION enforce_max_admins();
"""


def upgrade() -> None:
    op.execute(_FUNCTION_SQL)
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_enforce_max_admins ON users")
    op.execute("DROP FUNCTION IF EXISTS enforce_max_admins")
