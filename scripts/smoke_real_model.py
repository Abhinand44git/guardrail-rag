"""
End-to-end smoke test: the REAL embedder service (separate container, ADR-0012) + REAL Postgres +
the app's own HTTP routes. The embedder must be up (`docker compose up -d` waits for it).

Run inside the api container (the DATABASE_URL in .env points at the compose hostname `db`,
which only resolves there):

    docker compose exec api python -m scripts.smoke_real_model

What it proves that the test suite cannot (the suite uses a FAKE embedding backend): that the real
model's vectors survive ingestion -> storage -> search through the real HTTP routes, and that the
tenant / clearance / authorship rules still hold when the ranking is done by real semantic
similarity -- i.e. when the forbidden row really IS the best match.

Steps:
  1. log in as the seeded demo users (needs `python -m scripts.seed` first)
  2. ingest three documents through POST /ingest/document:
       A  Acme   "SMOKE: executive pay policy"   (default clearance = Admin-only)
       B  Acme   docs/BUILD_LOG.md, uploaded by a Developer, reclassified to level 3 by an Admin
       G  Globex "SMOKE: executive pay policy"   (same topic, different tenant)
  3. save project-memory notes through POST /project-memory (a Developer's default-level note,
     an Admin-only note, a level-3 note in another SDLC phase, and a Globex note)
  4. inspect the stored rows (model name, width, chunk strategy, tenant, author)
  5. ask the same question through POST /retrieve as every role, over EVERY visible row (k = 20)
  6. check phase narrowing, score ordering, retrieval quality, and timing
  7. clean up everything it created through the real DELETE routes (even if a check failed)

Exit code 0 = every check passed.

--fake-embeddings swaps in a hashed bag-of-words stand-in (no model, 768 wide). It exists only to
test this script itself; a run with it proves NOTHING about the real model.
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
import uuid
import zlib
from pathlib import Path

import httpx
from sqlalchemy import delete, func, select

from app.chunking import chunk_strategy_label
from app.database import AsyncSessionLocal
from app.embeddings import EmbeddingService, STORAGE_DIMS, get_embedding_service
from app.main import app
from app.models import Document, DocumentVector, ProjectMemory

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
# Memory notes. Every one starts with the prefix so a leftover from an interrupted run can be found.
NOTE_DEV = f"{TITLE_PREFIX} Decision: the executive bonus pool is capped at four percent of operating profit."
NOTE_ADMIN_ONLY = f"{TITLE_PREFIX} Confidential: the executive bonus pool formula and salary bands are under board review."
NOTE_OPEN = f"{TITLE_PREFIX} Testing note: bonus pool calculations are checked against the finance spreadsheet each quarter."
NOTE_GLOBEX = f"{TITLE_PREFIX} Globex decision: the executive bonus pool is paid in two instalments."

# A long, realistic document for the retrieval-quality check ("find the right chunk among a dozen").
# It must be STABLE: docs/BUILD_LOG.md is the old README kept as history, so it should not change.
# _long_document() below refuses to run, with an explanation, if that ever stops being true.
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
    """Hashed bag of words, exactly STORAGE_DIMS wide. For testing this script only."""
    native_dims = STORAGE_DIMS

    def encode(self, texts):
        out = []
        for t in texts:
            v = [0.0] * STORAGE_DIMS
            for w in re.findall(r"[a-z0-9_]+", t.lower()):
                v[zlib.crc32(w.encode()) % STORAGE_DIMS] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


async def login(client, email):
    r = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    if r.status_code != 200:
        sys.exit(f"login failed for {email} ({r.status_code}). Did you run `python -m scripts.seed`?")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def retrieve(client, headers, query, **extra):
    t0 = time.perf_counter()
    r = await client.post("/retrieve", headers=headers, json={"query": query, "k": 20, **extra})
    ms = (time.perf_counter() - t0) * 1000
    if r.status_code != 200:
        sys.exit(f"/retrieve returned {r.status_code}: {r.text[:200]}")
    return r.json(), ms


async def save_note(client, headers, project_id, phase, content, level=None):
    body = {"project_id": str(project_id), "phase": phase, "content": content}
    if level is not None:
        body["min_role_required"] = level
    return await client.post("/project-memory", headers=headers, json=body)


async def run(fake: bool) -> int:
    if fake:
        svc = EmbeddingService(FakeBackend(), "fake-hash-smoke")
        app.dependency_overrides[get_embedding_service] = lambda: svc
        print("!! --fake-embeddings: this run tests the SCRIPT, not the real model.")
    else:
        svc = get_embedding_service()  # the same singleton the routes use

    # leftovers from an interrupted earlier run
    async with AsyncSessionLocal() as db:
        await db.execute(delete(ProjectMemory).where(ProjectMemory.content.like(f"{TITLE_PREFIX}%")))
        await db.execute(delete(Document).where(Document.title.like(f"{TITLE_PREFIX}%")))
        await db.commit()

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://smoke", timeout=3600)
    docs_created: list[tuple[str, dict]] = []   # (header key, response) for cleanup
    notes_created: list[tuple[str, dict]] = []
    hdr = {}
    try:
        print("\n1. Logging in as seeded demo users")
        for key, email in {
            "acme_admin": "admin@acme-corp.local", "acme_dev": "developer@acme-corp.local",
            "acme_trainee": "trainee@acme-corp.local", "globex_admin": "admin@globex-inc.local",
        }.items():
            hdr[key] = await login(client, email)
        print("  ok")

        print("\n1b. First call to the embedder service (proves the API can reach it and that the model names match)")
        t0 = time.perf_counter()
        probe = svc.embed_query("warm-up")
        cold_s = time.perf_counter() - t0
        native, model = probe["embedding_dims"], probe["embedding_model"]
        print(f"  first request: {cold_s:.1f}s; model {model}, {native} dims")
        check(f"the model's native width equals the storage width ({STORAGE_DIMS})", native == STORAGE_DIMS, f"native {native}")

        print("\n2. Ingesting through the real routes")
        t0 = time.perf_counter()
        ra = await client.post("/ingest/document", headers=hdr["acme_admin"],
                               files={"file": ("pay.md", DOC_A.encode(), "text/markdown")},
                               data={"title": f"{TITLE_PREFIX} executive pay policy"})
        rg = await client.post("/ingest/document", headers=hdr["globex_admin"],
                               files={"file": ("pay.md", DOC_G.encode(), "text/markdown")},
                               data={"title": f"{TITLE_PREFIX} executive pay policy"})
        rb = await client.post("/ingest/document", headers=hdr["acme_dev"],
                               files={"file": ("BUILD_LOG.md", _long_document(), "text/markdown")},
                               data={"title": f"{TITLE_PREFIX} build log"})
        ingest_s = time.perf_counter() - t0
        for tag, r in (("A", ra), ("G", rg), ("B", rb)):
            if r.status_code != 201:
                check(f"ingest doc {tag} returns 201", False, f"got {r.status_code}: {r.text[:200]}")
                return 1
        a, g, b = ra.json(), rg.json(), rb.json()
        docs_created += [("acme_admin", a), ("globex_admin", g), ("acme_admin", b)]
        chunks_total = a["chunk_count"] + g["chunk_count"] + b["chunk_count"]
        check("ingest works for Admin and Developer", True,
              f"{chunks_total} chunks in {ingest_s:.1f}s = {ingest_s / chunks_total:.2f}s per chunk")
        check("new uploads default to Admin-only (min_role_required = 1)",
              a["min_role_required"] == 1 and g["min_role_required"] == 1 and b["min_role_required"] == 1)
        rp = await client.patch(f"/ingest/document/{b['id']}", headers=hdr["acme_admin"], json={"min_role_required": 3})
        check("Admin can reclassify the build log to level 3", rp.status_code == 200 and rp.json()["min_role_required"] == 3)
        rp2 = await client.patch(f"/ingest/document/{b['id']}", headers=hdr["acme_dev"], json={"min_role_required": 3})
        check("a Developer cannot reclassify", rp2.status_code in (401, 403), f"status {rp2.status_code}")
        acme, globex = a["tenant_id"], g["tenant_id"]
        check("the two tenants are different", acme != globex)

        print("\n3. Saving project memory through the real routes")
        proj = uuid.uuid4()
        rn1 = await save_note(client, hdr["acme_dev"], proj, "design", NOTE_DEV)                 # default level (1)
        rn2 = await save_note(client, hdr["acme_admin"], proj, "design", NOTE_ADMIN_ONLY, level=1)
        rn3 = await save_note(client, hdr["acme_admin"], proj, "testing", NOTE_OPEN, level=3)
        rn4 = await save_note(client, hdr["globex_admin"], proj, "design", NOTE_GLOBEX, level=3)
        rtr = await save_note(client, hdr["acme_trainee"], proj, "design", f"{TITLE_PREFIX} trainee note")
        for tag, r, key in (("dev note", rn1, "acme_dev"), ("admin-only note", rn2, "acme_admin"),
                            ("open note", rn3, "acme_admin"), ("globex note", rn4, "globex_admin")):
            if r.status_code != 201:
                check(f"save {tag} returns 201", False, f"got {r.status_code}: {r.text[:200]}")
                return 1
            notes_created.append((key, r.json()))
        n1, n2, n3, n4 = (x[1] for x in notes_created)
        check("a Trainee cannot write memory", rtr.status_code in (401, 403), f"status {rtr.status_code}")
        check("a Developer's note defaults to Admin-only", n1["min_role_required"] == 1)

        print("\n4. Inspecting stored rows")
        async with AsyncSessionLocal() as db:
            rows = (await db.execute(select(DocumentVector).where(DocumentVector.document_id == a["id"]))).scalars().all()
            mem = (await db.execute(select(ProjectMemory).where(ProjectMemory.id == n1["id"]))).scalar_one()
        r0 = rows[0]
        vec = list(r0.embedding)
        check("document: embedding_model stored correctly", r0.embedding_model == model, r0.embedding_model)
        check(f"document: vector is exactly {STORAGE_DIMS} wide and embedding_dims agrees",
              len(vec) == STORAGE_DIMS and r0.embedding_dims == native, f"len {len(vec)}, dims {r0.embedding_dims}")
        check("document: real (non-zero) values stored", any(x != 0 for x in vec))
        check("document: chunk_strategy recorded", r0.chunk_strategy == chunk_strategy_label(), r0.chunk_strategy)
        check("document: chunks belong to the right tenant", all(str(x.tenant_id) == acme for x in rows))
        check("memory: model, width, tenant and author recorded",
              mem.embedding_model == model and len(list(mem.embedding)) == STORAGE_DIMS
              and str(mem.tenant_id) == acme and str(mem.created_by_user_id) == n1["created_by_user_id"])
        norm_len = sum(x * x for x in vec) ** 0.5
        print(f"  (info) stored vector length {norm_len:.3f}: "
              + ("unit length." if abs(norm_len - 1) < 0.01 else "NOT unit length, which is why ranking is by cosine, never plain L2."))

        print("\n5. POST /retrieve as every role (k = 20, so every visible row is ranked)")
        ids = {"A": a["id"], "B": b["id"], "G": g["id"]}
        docs = lambda body: {d["document_id"] for d in body["documents"]}          # noqa: E731
        notes = lambda body: {m["memory_id"] for m in body["memory"]}              # noqa: E731

        body, ms_admin = await retrieve(client, hdr["acme_admin"], POLICY_QUESTION)
        d0 = body["documents"][0] if body["documents"] else None
        check("response names the embedding model", body["embedding_model"] == model)
        check("Acme Admin: best document is Acme's pay policy (A)", d0 is not None and d0["document_id"] == ids["A"],
              f"top-1 cosine similarity {d0['score']:.3f}" if d0 else "no rows")
        check("Acme Admin sees A and B, never Globex's document", docs(body) == {ids["A"], ids["B"]})
        check("Acme Admin sees all three Acme notes and no Globex note",
              notes(body) == {n1["id"], n2["id"], n3["id"]})
        for lst, key in ((body["documents"], "score"), (body["memory"], "score")):
            sc = [x[key] for x in lst]
            check("scores are in descending order and valid cosine values",
                  sc == sorted(sc, reverse=True) and all(-1.0001 <= s <= 1.0001 for s in sc))

        body, _ = await retrieve(client, hdr["acme_dev"], POLICY_QUESTION)
        check("Acme Developer: Admin-only doc A is invisible, though it is the best semantic match",
              ids["A"] not in docs(body) and docs(body) == {ids["B"]})
        check("Acme Developer: sees their OWN Admin-only note plus the open note, not the Admin's confidential one",
              notes(body) == {n1["id"], n3["id"]})

        body, _ = await retrieve(client, hdr["acme_trainee"], POLICY_QUESTION)
        check("Acme Trainee: only level-3 content (the build log and the open note)",
              docs(body) == {ids["B"]} and notes(body) == {n3["id"]})

        body, _ = await retrieve(client, hdr["globex_admin"], POLICY_QUESTION)
        d0 = body["documents"][0] if body["documents"] else None
        check("Globex Admin: sees only Globex's document, ranked first",
              docs(body) == {ids["G"]} and d0 is not None and d0["document_id"] == ids["G"])
        check("Globex Admin: sees only the Globex note, none of Acme's", notes(body) == {n4["id"]})

        print("\n6. Narrowing, quality and timing")
        body, _ = await retrieve(client, hdr["acme_admin"], POLICY_QUESTION, phase="design")
        check("phase=design includes design notes but not the later 'testing' note",
              notes(body) == {n1["id"], n2["id"]})
        body, _ = await retrieve(client, hdr["acme_admin"], POLICY_QUESTION, phase="planning")
        check("phase=planning finds nothing (no note is that early)", body["memory"] == [])
        body, _ = await retrieve(client, hdr["acme_admin"], POLICY_QUESTION, project_id=str(uuid.uuid4()))
        check("an unknown project_id narrows memory to nothing but does not widen or break anything",
              body["memory"] == [] and docs(body) == {ids["A"], ids["B"]})

        body, ms_quality = await retrieve(client, hdr["acme_trainee"], LONG_DOC_QUESTION)
        rank = next((i for i, d in enumerate(body["documents"], 1) if norm(LONG_DOC_EXPECTED) in norm(d["text"])), None)
        check("retrieval quality: a question about the build log finds the right chunk in the top 5",
              rank is not None and rank <= 5, f"first matching chunk at rank {rank} of {len(body['documents'])}")

        t0 = time.perf_counter(); svc.embed_query(POLICY_QUESTION); eq = (time.perf_counter() - t0) * 1000
        print(f"  embed one question (warm): {eq:.0f} ms | /retrieve end to end (Admin, k=20, includes embedding): "
              f"{ms_admin:.0f} ms | ingest: {ingest_s / chunks_total:.2f} s per chunk | cold start: {cold_s:.1f}s")
    finally:
        print("\n7. Cleaning up through the real DELETE routes")
        for key, n in notes_created:
            await client.delete(f"/project-memory/{n['id']}", headers=hdr["acme_admin"] if key != "globex_admin" else hdr["globex_admin"])
        for key, d in docs_created:
            await client.delete(f"/ingest/document/{d['id']}", headers=hdr[key])
        async with AsyncSessionLocal() as db:
            left_docs = (await db.execute(select(func.count()).select_from(Document)
                                          .where(Document.title.like(f"{TITLE_PREFIX}%")))).scalar_one()
            left_notes = (await db.execute(select(func.count()).select_from(ProjectMemory)
                                           .where(ProjectMemory.content.like(f"{TITLE_PREFIX}%")))).scalar_one()
            left_vecs = (await db.execute(select(func.count()).select_from(DocumentVector)
                                          .where(DocumentVector.document_id.in_([d["id"] for _, d in docs_created])))).scalar_one() if docs_created else 0
        check("cleanup left nothing behind (documents, their vectors, and memory notes)",
              left_docs == 0 and left_notes == 0 and left_vecs == 0)
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
