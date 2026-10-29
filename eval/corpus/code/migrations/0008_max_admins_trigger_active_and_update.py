"""trg_enforce_max_admins: count only active admins; also fire on promotion/reactivation

Two gaps closed here, both surfaced by Phase 3.5 adding new ways to become
an Admin beyond the original INSERT-only path migration 0002 covered:

1. **Counting.** Decision log (Phase 3.5 section): "deactivating an Admin
   DOES free their max_admins slot -- that's the actual point of allowing
   it." That requires this trigger to count is_active = true admins only,
   not every admin row ever created. Needs migration 0007's users.is_active
   column, hence this is a separate migration layered after it.

2. **Firing condition.** The original trigger only fires BEFORE INSERT.
   That was correct when the only way to become an Admin was
   POST /operator/tenants/{id}/admin (an INSERT). Phase 3.5 adds a second
   way: PATCH /admin/users/{id} can promote an existing Developer/Trainee
   to Admin via UPDATE, and (since PATCH also accepts is_active, filling a
   gap the source docs left open -- see decision log) can reactivate a
   previously-deactivated Admin via UPDATE. Neither of those is an INSERT,
   so the old trigger silently let both bypass the DB-level cap entirely --
   an admin-cap enforcement gap the app-level pre-check
   (user_provisioning.check_admin_cap_or_raise) does NOT close on its own,
   since a trigger is the race-safety net underneath that pre-check, not a
   substitute for it (same "belt and suspenders" reasoning as migration
   0002 and the operator router's use of it).

   Extended to `BEFORE INSERT OR UPDATE OF role, is_active` and taught to
   recognize four transitions:
     - INSERT of a new active Admin row               -> checked
     - UPDATE promoting Developer/Trainee -> Admin      -> checked
     - UPDATE reactivating a deactivated Admin          -> checked
     - UPDATE demoting/deactivating an Admin            -> never checked
       (cap can only be violated by *gaining* an active admin, not losing
       one; NEW.role <> 1 or NEW.is_active IS NOT TRUE returns early)
     - UPDATE that re-sends role=1/is_active=true on a row that was
       already an active Admin (no real transition, e.g. an unrelated
       field changed alongside these two in the same statement) -> never
       checked, since it does not add a new active Admin to the count

   Validated interactively against a real Postgres instance before being
   committed here as a migration (all four transitions above, plus the
   INSERT-context OLD-reference safety of the short-circuited boolean
   guard) -- not just reasoned about.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-12

"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

_OLD_FUNCTION_SQL = """
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
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_NEW_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION enforce_max_admins() RETURNS TRIGGER AS $$
DECLARE
    admin_count INTEGER;
    cap INTEGER;
    was_active_admin BOOLEAN;
BEGIN
    -- Only rows that are (or are becoming) an ACTIVE Admin can violate the
    -- cap. A demotion, a deactivation, or a row that was never/isn't
    -- becoming an active Admin is always allowed through untouched.
    IF NEW.role <> 1 OR NEW.is_active IS NOT TRUE THEN
        RETURN NEW;
    END IF;

    -- Was this row already an active Admin before this statement? If so,
    -- it's already counted below -- this update isn't adding a new active
    -- Admin (e.g. role/is_active re-sent unchanged alongside some other
    -- field). Short-circuit AND means OLD is never dereferenced when
    -- TG_OP = 'INSERT' (OLD does not exist yet in that context).
    was_active_admin := (TG_OP = 'UPDATE' AND OLD.role = 1 AND OLD.is_active IS TRUE);
    IF was_active_admin THEN
        RETURN NEW;
    END IF;

    SELECT max_admins INTO cap FROM tenants WHERE id = NEW.tenant_id;

    -- Active admins only -- a deactivated Admin no longer counts against
    -- the cap (decision log: deactivating an Admin frees their slot).
    SELECT COUNT(*) INTO admin_count
    FROM users
    WHERE tenant_id = NEW.tenant_id AND role = 1 AND is_active = true;

    IF admin_count >= cap THEN
        RAISE EXCEPTION 'tenant % already has % active admin(s), max_admins is %',
            NEW.tenant_id, admin_count, cap
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(_NEW_FUNCTION_SQL)
    # Must drop and recreate: the trigger's fired-on-column-list
    # (`OF role, is_active`) is part of the trigger definition itself, not
    # the function -- CREATE OR REPLACE FUNCTION alone doesn't change it.
    op.execute("DROP TRIGGER IF EXISTS trg_enforce_max_admins ON users")
    op.execute(
        """
        CREATE TRIGGER trg_enforce_max_admins
            BEFORE INSERT OR UPDATE OF role, is_active ON users
            FOR EACH ROW
            EXECUTE FUNCTION enforce_max_admins();
        """
    )


def downgrade() -> None:
    op.execute(_OLD_FUNCTION_SQL)
    op.execute("DROP TRIGGER IF EXISTS trg_enforce_max_admins ON users")
    op.execute(
        """
        CREATE TRIGGER trg_enforce_max_admins
            BEFORE INSERT ON users
            FOR EACH ROW
            EXECUTE FUNCTION enforce_max_admins();
        """
    )
