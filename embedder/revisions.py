"""
Revision lock for the embedder image (ADR-0012). Deliberately free of heavy imports
(no torch, no transformers) so it can be unit-tested anywhere and imported by both the
build-time script and the runtime service.

The lock file is the single place that says which Hugging Face commits the image is
made of. The build downloads exactly those, then proves the model loads with the network
disabled; at run time the container never contacts Hugging Face at all.
"""
from __future__ import annotations

import re
from pathlib import Path

UNLOCKED = "UNLOCKED"
_SHA_RE = re.compile(r"[0-9a-f]{40}")


def parse_lock(text: str) -> dict[str, str]:
    """'<repo> <40-hex commit | UNLOCKED>' per line; '#' comments and blank lines ignored."""
    lock: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"revisions.lock line {number}: expected '<repo> <commit>', got {raw!r}")
        repo, commit = parts
        if repo in lock:
            raise ValueError(f"revisions.lock line {number}: {repo} is listed twice")
        if commit != UNLOCKED and not _SHA_RE.fullmatch(commit):
            raise ValueError(
                f"revisions.lock line {number}: {commit!r} is not a full 40-character commit "
                f"hash (short hashes and branch names such as 'main' are not pins)"
            )
        lock[repo] = commit
    return lock


def load_lock(path: str | Path) -> dict[str, str]:
    return parse_lock(Path(path).read_text(encoding="utf-8"))


def revisions_for(lock: dict[str, str], model_name: str) -> tuple[str | None, str | None]:
    """
    (model_revision, code_revision) to hand to the loader. model_revision is the lock entry
    for the model repo itself; code_revision is the entry for the model's remote-code repo,
    which is taken to be the single other repo in the file. Either is None when absent or
    UNLOCKED, meaning "not pinned".
    """
    def pinned(value: str | None) -> str | None:
        return None if value in (None, UNLOCKED) else value

    others = [repo for repo in lock if repo != model_name]
    code = lock[others[0]] if len(others) == 1 else None
    return pinned(lock.get(model_name)), pinned(code)


def scan_cached(hub_dir: str | Path) -> dict[str, set[str]]:
    """repo id -> commits present in a Hugging Face hub cache directory (<HF_HOME>/hub)."""
    found: dict[str, set[str]] = {}
    for repo_dir in Path(hub_dir).glob("models--*"):
        snapshots = repo_dir / "snapshots"
        if not snapshots.is_dir():
            continue
        repo = repo_dir.name[len("models--"):].replace("--", "/")
        commits = {p.name for p in snapshots.iterdir() if p.is_dir()}
        if commits:
            found[repo] = commits
    return found


def check(lock: dict[str, str], cached: dict[str, set[str]]) -> list[str]:
    """Differences between what the lock says and what is actually in the image's cache."""
    problems: list[str] = []
    for repo, commits in sorted(cached.items()):
        listed = ", ".join(sorted(commits))
        if repo not in lock:
            problems.append(f"{repo} was downloaded but is not in revisions.lock (resolved to {listed})")
        elif lock[repo] == UNLOCKED:
            problems.append(f"{repo} is UNLOCKED; this build resolved it to {listed} -- put that in revisions.lock")
        elif commits != {lock[repo]}:
            problems.append(f"{repo}: revisions.lock says {lock[repo]} but the image holds {listed}")
    for repo in sorted(set(lock) - set(cached)):
        problems.append(f"{repo} is in revisions.lock but nothing from it was loaded")
    return problems
