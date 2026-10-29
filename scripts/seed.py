"""
Seed script for local/dev use.

Creates:
  - 1 operator
  - 2 demo tenants
  - 3 fixture users per tenant (one per role: Admin, Developer, Trainee)
  - 3 fixture documents per tenant (one per min_role_required: 1, 2, 3)

The fixture users/docs are the exact fixtures the Phase 2 RBAC unit test
needs (see decision log: "one fixture user per role, one fixture doc per
sensitivity level").

Run inside the api container:
    docker compose exec api python -m scripts.seed
"""
import asyncio
import uuid

from sqlalchemy import select

from app.database import AsyncSessionLocal, engine, Base
from app.models import Operator, Tenant, User, Document, Role

DEMO_PASSWORD = "ChangeMe123!"  # dev-only fixture password, never used outside local seed


async def seed():
    async with engine.begin() as conn:
        # Dev convenience only — real schema management is via alembic upgrade head.
        await conn.run_sync(Base.metadata.create_all)

    from app.security import hash_password

    async with AsyncSessionLocal() as db:
        existing = await db.execute(select(Operator).limit(1))
        if existing.scalar_one_or_none():
            print("Seed data already present — skipping.")
            return

        operator = Operator(
            id=uuid.uuid4(),
            username="root_operator",
            email="operator@guardrail.local",
            hashed_password=hash_password(DEMO_PASSWORD),
        )
        db.add(operator)

        tenants = []
        for name in ("Acme Corp", "Globex Inc"):
            tenant = Tenant(id=uuid.uuid4(), name=name, max_admins=1)
            db.add(tenant)
            tenants.append(tenant)

        await db.flush()  # populate tenant.id before FK use below

        role_labels = {
            Role.ADMIN: "admin",
            Role.DEVELOPER: "developer",
            Role.TRAINEE: "trainee",
        }

        for tenant in tenants:
            slug = tenant.name.lower().replace(" ", "-")

            for role, label in role_labels.items():
                db.add(User(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    username=f"{label}@{slug}",
                    email=f"{label}@{slug}.local",
                    hashed_password=hash_password(DEMO_PASSWORD),
                    role=role.value,
                ))

            for role, label in role_labels.items():
                db.add(Document(
                    id=uuid.uuid4(),
                    tenant_id=tenant.id,
                    title=f"{label.title()}-only doc ({tenant.name})",
                    # Phase 4: source_type is now required (migration 0010).
                    # These are RBAC-fixture docs, not real ingested content
                    # (no document_vectors rows of their own) -- 'markdown'
                    # picked for the same "most neutral of the three allowed
                    # values" reasoning as that migration's backfill.
                    source_type="markdown",
                    min_role_required=role.value,
                ))

        await db.commit()

    print("Seed complete: 1 operator, 2 tenants, 3 users/tenant, 3 documents/tenant.")
    print(f"All fixture passwords: {DEMO_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(seed())
