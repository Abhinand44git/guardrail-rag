"""
The embedder's revision lock (ADR-0012): which exact Hugging Face commits the image is made of.
Pure file/dict logic, so it is tested for real, with no network and no model.
"""
from pathlib import Path

import pytest

from embedder import fetch_model
from embedder.revisions import UNLOCKED, check, load_lock, parse_lock, revisions_for, scan_cached

ROOT = Path(__file__).resolve().parents[1]
MODEL = "nomic-ai/nomic-embed-text-v1.5"
CODE = "nomic-ai/nomic-bert-2048"
A, B, C = "a" * 40, "b" * 40, "c" * 40


def test_parse_ignores_comments_and_blank_lines():
    text = f"# header\n\n{MODEL}  {A}   # trailing comment\n{CODE} {UNLOCKED}\n"
    assert parse_lock(text) == {MODEL: A, CODE: UNLOCKED}


@pytest.mark.parametrize("bad", [
    f"{MODEL} main",                 # a branch name is not a pin
    f"{MODEL} {A[:12]}",             # a short hash is not a pin
    f"{MODEL}",                      # missing commit
    f"{MODEL} {A} extra",            # too many fields
    f"{MODEL} {A}\n{MODEL} {B}",     # same repo twice
])
def test_parse_rejects_anything_that_is_not_a_full_commit_hash(bad):
    with pytest.raises(ValueError):
        parse_lock(bad)


def test_the_shipped_lock_pins_both_repositories_to_the_commits_the_real_eval_used():
    """Both commits come from eval/results/*.json -- the combination actually run on a real
    machine with the pinned library versions. Neither may be left UNLOCKED."""
    lock = load_lock(ROOT / "embedder" / "revisions.lock")
    assert lock == {
        MODEL: "e9b6763023c676ca8431644204f50c2b100d9aab",
        CODE: "7710840340a098cfb869c4f65e87cf2b1b70caca",
    }


def test_revisions_for_returns_model_and_code_pins_and_none_when_unlocked():
    assert revisions_for({MODEL: A, CODE: B}, MODEL) == (A, B)
    assert revisions_for({MODEL: A, CODE: UNLOCKED}, MODEL) == (A, None)
    assert revisions_for({MODEL: UNLOCKED}, MODEL) == (None, None)
    assert revisions_for({}, MODEL) == (None, None)


def _make_cache(tmp_path: Path, repos: dict[str, list[str]]) -> Path:
    for repo, commits in repos.items():
        for commit in commits:
            (tmp_path / f"models--{repo.replace('/', '--')}" / "snapshots" / commit).mkdir(parents=True)
    return tmp_path


def test_scan_cached_reads_repo_names_and_commits_from_a_hub_cache(tmp_path):
    hub = _make_cache(tmp_path, {MODEL: [A], CODE: [B]})
    (hub / "version.txt").write_text("x")  # stray non-repo files are ignored
    assert scan_cached(hub) == {MODEL: {A}, CODE: {B}}


def test_check_is_quiet_when_the_image_matches_the_lock():
    assert check({MODEL: A, CODE: B}, {MODEL: {A}, CODE: {B}}) == []


def test_check_reports_every_kind_of_drift():
    problems = check(
        {MODEL: A, CODE: UNLOCKED, "org/never-loaded": C},
        {MODEL: {B}, CODE: {B}, "org/surprise": {C}},
    )
    joined = "\n".join(problems)
    assert f"{MODEL}: revisions.lock says {A} but the image holds {B}" in joined   # drifted
    assert f"{CODE} is UNLOCKED; this build resolved it to {B}" in joined          # unpinned
    assert "org/surprise was downloaded but is not in revisions.lock" in joined    # unexpected download
    assert "org/never-loaded is in revisions.lock but nothing from it was loaded" in joined
    assert len(problems) == 4


def test_offline_verification_refuses_to_run_with_the_network_enabled(monkeypatch):
    """The whole point of that build step is proving the model loads WITHOUT the network; if the
    switch is not set it must stop immediately instead of quietly verifying nothing."""
    monkeypatch.setenv("EMBEDDER_MODEL_NAME", MODEL)
    monkeypatch.setenv("EMBEDDER_EXPECTED_DIMS", "768")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    assert fetch_model.main(["--verify-offline"]) == 2
