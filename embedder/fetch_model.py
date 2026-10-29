"""
Build-time script for the embedder image (ADR-0012). Never runs in the live service.

  python -m embedder.fetch_model                  # step 1, ONLINE: download weights + remote code
  HF_HUB_OFFLINE=1 python -m embedder.fetch_model --verify-offline
                                                  # step 2: load with the network switched off,
                                                  # encode a sentence, check dims and the lock

Step 2 turns "it works as long as Hugging Face is reachable" into a failed docker build. If
the model cannot load offline, no image is produced -- rather than an image that fails at 3 a.m.
Set REVISION_LOCK_MODE=strict to also fail the build when the lock file and the cache disagree
(default: warn, so the first build can tell you what to put in the lock).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from embedder.backend import SentenceTransformerBackend
from embedder.revisions import check, load_lock, revisions_for, scan_cached

LOCK_PATH = Path(__file__).with_name("revisions.lock")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-offline", action="store_true")
    args = ap.parse_args(argv)

    name = os.environ["EMBEDDER_MODEL_NAME"]
    expected_dims = int(os.environ["EMBEDDER_EXPECTED_DIMS"])
    strict = os.environ.get("REVISION_LOCK_MODE", "warn").lower() == "strict"
    if args.verify_offline and os.environ.get("HF_HUB_OFFLINE") != "1":
        print("refusing to verify: HF_HUB_OFFLINE=1 must be set for this step", file=sys.stderr)
        return 2

    lock = load_lock(LOCK_PATH)
    model_revision, code_revision = revisions_for(lock, name)
    print(f"model={name} model_revision={model_revision or 'UNPINNED'} code_revision={code_revision or 'UNPINNED'}")

    backend = SentenceTransformerBackend(
        name,
        model_revision=model_revision,
        code_revision=code_revision,
        trust_remote_code=os.environ.get("EMBEDDER_TRUST_REMOTE_CODE", "true").lower() == "true",
    )
    backend.load()
    vector = backend.encode(["search_document: build-time self test"])[0]
    if backend.dims != expected_dims or len(vector) != expected_dims:
        print(f"FAIL: model produced {len(vector)} dims, expected {expected_dims}", file=sys.stderr)
        return 1
    print(f"ok: loaded and encoded ({backend.dims} dims){' with the network disabled' if args.verify_offline else ''}")

    if args.verify_offline:
        problems = check(lock, scan_cached(Path(os.environ["HF_HOME"]) / "hub"))
        for p in problems:
            print(("FAIL: " if strict else "WARNING: ") + p, file=sys.stderr)
        if problems and strict:
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
