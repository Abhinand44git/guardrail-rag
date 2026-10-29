"""users: unique (tenant_id, username), not global

username had no uniqueness constraint at all -- the same gap class as
migration 0003 (users.email), found the same way: by inspection, not by an
incident report, but the same shape of latent bug.

Deliberately scoped to (tenant_id, username), not username alone. Unlike
email, username is never used to look a user up (auth/login resolves by
email only) -- there is no structural reason it needs to be globally
unique, and making it globally unique would force every tenant's users to
coordinate naming against every OTHER tenant's users, which is nonsensical
for unrelated companies. Per-tenant uniqueness means "admin" can exist as a
username in every tenant simultaneously; it only has to be distinct within
one company.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-31

"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_users_tenant_username", "users", ["tenant_id", "username"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_users_tenant_username", "users", type_="unique")
