"""
End-to-end smoke test: the REAL embedder service (separate container, ADR-0012) + REAL
Postgres + the app's own code. The embedder must be up (`docker compose up -d` waits for it).

Run inside the api container (the DATABASE_URL in .env points at the compose
hostname `db`, which only resolves there):

    docker compose exec api python -m scripts.smoke_real_model

What it proves that the 112-test suite cannot (the suite uses a FAKE embedding
backend): that the real model's vectors survive ingestion -> storage -> search,
and that the RBAC / tenant filter still holds when the ranking is done by real
semantic similarity -- i.e. when the forbidden chunk really IS the best match.

Steps:
  1. log in as the seeded demo users (needs `python -m scripts.seed` first)
  2. ingest three documents through the real HTTP routes (in-process ASGI):
       A  Acme   "SMOKE: executive pay policy"   (default clearance = Admin-only)
       B  Acme   docs/BUILD_LOG.md, uploaded by a Developer, reclassified to level 3 by an Admin
       G  Globex "SMOKE: executive pay policy"   (same topic, different tenant)
  3. inspect the stored rows (model name, dims, zero-padding, chunk strategy)
  4. search as each role using the app's own embed_query() and
     visible_document_vectors_query(), over EVERY visible chunk (not just the
     top few), so a leak anywhere in the ranking is caught
  5. clean up everything it created (even if a check failed)

Exit code 0 = every check passed. This is a smoke test, not the Phase 5 /query
endpoint: that endpoint does not exist yet; this uses the same building blocks.

--fake-embeddings swaps in a hashed bag-of-words stand-in (no model download).
It exists only to test this script itself; a run with it proves NOTHING about
the real model.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import zlib
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select

from app.chunking import chunk_strategy_label
from app.database import AsyncSessionLocal
from app.embeddings import EmbeddingService, STORAGE_DIMS, get_embedding_service
from app.main import app
from app.models import Document, DocumentVector
from app.rbac import visible_document_vectors_query

ROOT = Path(__file__).resolve().parent.parent
PASSWORD = "ChangeMe123!"  # the seed script's fixture password
TITLE_PREFIX = "SMOKE:"
POLICY_QUESTION = "How is the executive bonus pool calculated?"
DOC_A = (
    "# Executive compensation policy (Acme)\n\n"
    "The annual executive bonus pool equals four percent of operating profit. "
    "The board remuneration committee approves the pool each March, and no "
    "individual executive may receive more than twenty percent of it.\n"
)
DOC_G = (
    "# Executive compensation policy (Globex)\n\n"
    "At Globex the executive bonus pool is nine percent of net revenue, set by "
    "the chief financial officer every January and paid in two instalments.\n"
)
# A long, realistic document for the retrieval-quality check ("find the right chunk among a dozen").
# It must be STABLE: this used to be README.md, and rewriting the README for GitHub silently broke
# the check (the expected passage was gone, and the failure looked like a retrieval regression).
# docs/BUILD_LOG.md is the old README kept as history, so it should not change. _long_document()
# below refuses to run, with an explanation, if that ever stops being true.
LONG_DOC_PATH = ROOT / "docs" / "BUILD_LOG.md"
LONG_DOC_QUESTION = "Why does the app validate the Fernet key at startup instead of when a tenant first saves a key?"
LONG_DOC_EXPECTED = "is a well-formed Fernet key at process"  # whitespace-insensitive match

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).lower()


def _long_document() -> bytes:
    """Load the long fixture document, or stop with a clear explanation if it cannot do its job."""
    if not LONG_DOC_PATH.exists():
        sys.exit(f"smoke test needs {LONG_DOC_PATH.relative_to(ROOT)} (the old README kept as history) but it is missing")
    data = LONG_DOC_PATH.read_bytes()
    if norm(LONG_DOC_EXPECTED) not in norm(data.decode("utf-8", errors="replace")):
        sys.exit(
            f"{LONG_DOC_PATH.relative_to(ROOT)} no longer contains the passage this check searches for "
            f"({LONG_DOC_EXPECTED!r}). Update LONG_DOC_EXPECTED/LONG_DOC_QUESTION to match the document; "
            f"this is a test-fixture problem, not a retrieval failure."
        )
    return data


class FakeBackend:
    """Hashed bag of words, 256 dims. For testing this script only."""
    native_dims = 256

    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 256
            for w in re.findall(r"[a-z0-9_]+", t.lower()):
                v[zlib.crc32(w.encode()) % 256] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


async def login(client, email):
    r = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    if r.status_code != 200:
        sys.exit(f"login failed for {email} ({r.status_code}). Did you run `python -m scripts.seed`?")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def search(svc, tenant_id, role, question, model_filter=None):
    """The building blocks Phase 5 will use: the app's embed_query, the app's
    RBAC query, ranked by cosine distance, over EVERY visible chunk."""
    q = svc.embed_query(question)
    vec = q["embedding"]
    dist = DocumentVector.embedding.cosine_distance(vec)
    stmt = (
        visible_document_vectors_query(tenant_id, role)
        .where(DocumentVector.embedding_model == (model_filter or q["embedding_model"]))
        .add_columns(Document.title, dist.label("dist"))
        .order_by(dist)
        .limit(1000)
    )
    async with AsyncSessionLocal() as db:
        t0 = time.perf_counter()
        rows = (await db.execute(stmt)).all()
        ms = (time.perf_counter() - t0) * 1000
    return rows, ms


async def run(fake: bool) -> int:
    if fake:
        svc = EmbeddingService(FakeBackend(), "fake-hash-smoke")
        app.dependency_overrides[get_embedding_service] = lambda: svc
        print("!! --fake-embeddings: this run tests the SCRIPT, not the real model.")
    else:
        svc = get_embedding_service()  # the same singleton the ingest route uses

    # leftovers from an interrupted earlier run
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Document).where(Document.title.like(f"{TITLE_PREFIX}%")))
        await db.commit()

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smoke", timeout=3600)
    created: list[tuple[str, dict]] = []  # (admin-header-key, response) for cleanup
    hdr = {}
    try:
        print("\n1. Logging in as seeded demo users")
        for key, email in {
            "acme_admin": "admin@acme-corp.local", "acme_dev": "developer@acme-corp.local",
            "acme_trainee": "trainee@acme-corp.local", "globex_admin": "admin@globex-inc.local",
        }.items():
            hdr[key] = await login(client, email)
        print("  ok")

        print("\n1b. First call to the embedder service (the model is already loaded inside it; this also proves the API can reach it and that the model names match)")
        t0 = time.perf_counter()
        svc.embed_query("warm-up")
        cold_s = time.perf_counter() - t0
        print(f"  first request: {cold_s:.1f}s")

        print("\n2. Ingesting through the real routes")
        t0 = time.perf_counter()
        ra = await client.post("/ingest/document", headers=hdr["acme_admin"],
                               files={"file": ("pay.md", DOC_A.encode(), "text/markdown")},
                               data={"title": f"{TITLE_PREFIX} executive pay policy"})
        rg = await client.post("/ingest/document", headers=hdr["globex_admin"],
                               files={"file": ("pay.md", DOC_G.encode(), "text/markdown")},
                               data={"title": f"{TITLE_PREFIX} executive pay policy"})
        long_doc = _long_document()
        rb = await client.post("/ingest/document", headers=hdr["acme_dev"],
                               files={"file": ("BUILD_LOG.md", long_doc, "text/markdown")},
                               data={"title": f"{TITLE_PREFIX} build log"})
        ingest_s = time.perf_counter() - t0
        for tag, key, r in (("A", "acme_admin", ra), ("G", "globex_admin", rg), ("B", "acme_dev", rb)):
            if r.status_code != 201:
                check(f"ingest doc {tag} returns 201", False, f"got {r.status_code}: {r.text[:200]}")
                return 1
        a, g, b = ra.json(), rg.json(), rb.json()
        created += [("acme_admin", a), ("globex_admin", g), ("acme_admin", b)]
        chunks_total = a["chunk_count"] + g["chunk_count"] + b["chunk_count"]
        check("ingest works for Admin and Developer; Developer can upload", True,
              f"{chunks_total} chunks in {ingest_s:.1f}s = {ingest_s / chunks_total:.2f}s per chunk")
        check("new uploads default to Admin-only (min_role_required = 1)",
              a["min_role_required"] == 1 and g["min_role_required"] == 1 and b["min_role_required"] == 1)
        rp = await client.patch(f"/ingest/document/{b['id']}", headers=hdr["acme_admin"],
                                json={"min_role_required": 3})
        check("Admin can reclassify the build log to level 3", rp.status_code == 200 and rp.json()["min_role_required"] == 3)
        rp2 = await client.patch(f"/ingest/document/{b['id']}", headers=hdr["acme_dev"], json={"min_role_required": 3})
        check("a Developer cannot reclassify", rp2.status_code in (401, 403), f"status {rp2.status_code}")

        acme, globex = a["tenant_id"], g["tenant_id"]
        check("the two tenants are different", acme != globex)

        print("\n3. Inspecting stored rows")
        probe = svc.embed_query("probe")
        native, model = probe["embedding_dims"], probe["embedding_model"]
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(DocumentVector).where(DocumentVector.document_id == a["id"]))).scalars().all()
        r0 = rows[0]
        vec = list(r0.embedding)
        check("embedding_model stored correctly", r0.embedding_model == model, r0.embedding_model)
        check(f"true dimensionality recorded ({native})", r0.embedding_dims == native, f"stored {r0.embedding_dims}")
        check(f"vector stored at {STORAGE_DIMS} wide", len(vec) == STORAGE_DIMS, f"len {len(vec)}")
        check("padding beyond the true dims is all zeros", all(x == 0 for x in vec[native:]))
        check("real values within the true dims are non-zero", any(x != 0 for x in vec[:native]))
        check("chunk_strategy recorded", r0.chunk_strategy == chunk_strategy_label(), r0.chunk_strategy)
        check("chunks belong to the right tenant", all(str(x.tenant_id) == acme for x in rows))
        norm_len = sum(x * x for x in vec) ** 0.5
        print(f"  (info) stored vector length {norm_len:.3f}: "
              + ("unit length." if abs(norm_len - 1) < 0.01 else
                 "NOT unit length, so Phase 5 must rank by cosine distance, never by plain L2 distance."))

        print("\n4. Searching as each role (ranking over EVERY visible chunk)")
        ids = {"A": a["id"], "B": b["id"], "G": g["id"]}

        async def docs_seen(tenant, role, q):
            rws, ms = await search(svc, tenant, role, q)
            return rws, ms, {str(r[0].document_id) for r in rws}

        rws, ms_admin, seen = await docs_seen(acme, 1, POLICY_QUESTION)
        check("Acme Admin: best match is Acme's pay policy (A)", bool(rws) and str(rws[0][0].document_id) == ids["A"],
              f"top-1 cosine similarity {1 - rws[0].dist:.3f}" if rws else "no rows")
        check("Acme Admin sees A and B, never Globex's document", seen == {ids["A"], ids["B"]}, f"saw {len(rws)} chunks")
        _, _, seen = await docs_seen(acme, 2, POLICY_QUESTION)
        check("Acme Developer: Admin-only doc A is invisible, though it is the best semantic match",
              ids["A"] not in seen and seen == {ids["B"]})
        rws_t, _, seen = await docs_seen(acme, 3, POLICY_QUESTION)
        check("Acme Trainee: only level-3 content (the build log), nothing else",
              seen == {ids["B"]} and ids["G"] not in seen and ids["A"] not in seen, f"saw {len(rws_t)} chunks")
        rws_g, _, seen = await docs_seen(globex, 1, POLICY_QUESTION)
        check("Globex Admin: sees only Globex's document, ranked first",
              seen == {ids["G"]} and str(rws_g[0][0].document_id) == ids["G"])
        rws_x, _ = await search(svc, acme, 1, POLICY_QUESTION, model_filter="some-other-model")
        check("rows from a different embedding_model are excluded", len(rws_x) == 0)

        rws_r, ms_readme, _ = await docs_seen(acme, 3, LONG_DOC_QUESTION)
        rank = next((i for i, r in enumerate(rws_r, 1) if norm(LONG_DOC_EXPECTED) in norm(r[0].chunk_text)), None)
        check("retrieval quality: a question about the build log finds the right chunk in the top 5",
              rank is not None and rank <= 5, f"first matching chunk at rank {rank} of {len(rws_r)}")

        print("\n5. Timing")
        t0 = time.perf_counter(); svc.embed_query(POLICY_QUESTION); eq = (time.perf_counter() - t0) * 1000
        print(f"  embed one question (warm): {eq:.0f} ms | database search over all visible chunks "
              f"(Admin): {ms_admin:.0f} ms | ingest: {ingest_s / chunks_total:.2f} s per chunk "
              f"(model already warm) | cold start: {cold_s:.1f}s")
    finally:
        print("\n6. Cleaning up")
        for key, d in created:
            await client.delete(f"/ingest/document/{d['id']}", headers=hdr[key])
        async with AsyncSessionLocal() as db:
            left_docs = (await db.execute(select(func.count()).select_from(Document)
                                          .where(Document.title.like(f"{TITLE_PREFIX}%")))).scalar_one()
            left_vecs = (await db.execute(select(func.count()).select_from(DocumentVector)
                                          .where(DocumentVector.document_id.in_([d["id"] for _, d in created])))).scalar_one() if created else 0
        check("cleanup left nothing behind (documents and their vectors)", left_docs == 0 and left_vecs == 0)
        await client.aclose()

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fake-embeddings", action="store_true", help="test this script only; proves nothing about the real model")
    sys.exit(asyncio.run(run(ap.parse_args().fake_embeddings)))


if __name__ == "__main__":
    main()