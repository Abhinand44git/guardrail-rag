"""
A short, repeatable demo of the core idea, over real HTTP against a RUNNING stack.

    docker compose up -d                       # then, from your own machine (needs `pip install httpx`):
    python -m scripts.demo_retrieve            # defaults to http://localhost:8000 and the seeded demo users
    python -m scripts.demo_retrieve --keep     # leave the demo data in place so you can poke at /docs

What it shows, in about ten lines of output:
  * an Admin saves a confidential document (Admin-only) and a harmless one (visible to everyone),
    plus a project-memory note;
  * the SAME question is asked as an Admin and as a Trainee through POST /retrieve;
  * the Admin gets both documents; the Trainee gets only the harmless one, even though the
    confidential one is the better match for the question. The filter is in the SQL query,
    before ranking, so there is nothing to talk the system out of.

It uses the seeded fixture users (python -m scripts.seed) and removes what it created unless --keep.
"""
from __future__ import annotations

import argparse
import sys
import uuid

import httpx

PASSWORD = "ChangeMe123!"  # the seed script's fixture password (local development only)
CONFIDENTIAL = ("Confidential salary bands: a senior engineer earns between ninety and one hundred "
                "twenty thousand euros, and the engineering director earns about one hundred eighty thousand.")
HARMLESS = ("Onboarding guide: new engineers get a laptop on day one, meet their mentor on day two, "
            "and join the weekly planning meeting on Friday.")
NOTE = "Decision: salary questions go to the people team, never to the engineering channel."
QUESTION = "How much does a senior engineer earn?"


def login(client: httpx.Client, email: str) -> dict:
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD})
    if r.status_code != 200:
        sys.exit(f"login failed for {email} ({r.status_code}): is the stack up and seeded (python -m scripts.seed)?")
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def show(label: str, body: dict) -> None:
    print(f"\n{label}")
    for d in body["documents"]:
        print(f"   document  {d['score']:.3f}  {d['title']}")
    for m in body["memory"]:
        print(f"   memory    {m['score']:.3f}  {m['text'][:70]}")
    if not body["documents"] and not body["memory"]:
        print("   (nothing)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--keep", action="store_true", help="leave the demo data in place")
    args = ap.parse_args()

    created_docs, created_notes = [], []
    with httpx.Client(base_url=args.base_url, timeout=600) as c:
        admin = login(c, "admin@acme-corp.local")
        trainee = login(c, "trainee@acme-corp.local")
        try:
            print("Saving a confidential document (Admin-only), a harmless one (everyone) and a note ...")
            for title, text, level in (("DEMO: salary bands", CONFIDENTIAL, 1), ("DEMO: onboarding guide", HARMLESS, 3)):
                r = c.post("/ingest/code", headers=admin, json={"title": title, "content": text, "min_role_required": level})
                r.raise_for_status()
                created_docs.append(r.json()["id"])
            r = c.post("/project-memory", headers=admin, json={
                "project_id": str(uuid.uuid4()), "phase": "design", "content": NOTE, "min_role_required": 3})
            r.raise_for_status()
            created_notes.append(r.json()["id"])

            print(f'\nQuestion: "{QUESTION}"')
            for label, headers in (("As an ADMIN:", admin), ("As a TRAINEE (lowest clearance):", trainee)):
                r = c.post("/retrieve", headers=headers, json={"query": QUESTION, "k": 5})
                r.raise_for_status()
                show(label, r.json())
            print("\nThe Trainee never sees the salary document, although it answers the question best.")
        finally:
            if not args.keep:
                for n in created_notes:
                    c.delete(f"/project-memory/{n}", headers=admin)
                for d in created_docs:
                    c.delete(f"/ingest/document/{d}", headers=admin)
                print("\n(cleaned up; use --keep to leave the data in place)")


if __name__ == "__main__":
    main()
