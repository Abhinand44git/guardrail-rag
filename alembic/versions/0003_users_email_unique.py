"""users.email unique constraint

/auth/login looks a user up by email alone (no tenant identifier in the
request), so it implicitly assumes email resolves to exactly one row across
the whole users table -- but Phase 1 shipped only a plain (non-unique)
index. Found during Phase 2 testing: two rows sharing an email made
/auth/login raise MultipleResultsFound (500) instead of authenticating,
instead of the ambiguity being impossible in the first place.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-16

"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop the old plain index and replace it with a unique one -- same
    # column, so lookups get the same index benefit plus the constraint.
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_email", table_name="users")
    op.create_index("ix_users_email", "users", ["email"], unique=False)
