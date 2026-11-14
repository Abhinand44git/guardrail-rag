"""
Batch D, Round 1: the fusion maths in eval/run.py (offline measurement, not app code).

Pure logic, no database. The expected values below are worked out by hand, because a fusion
function that "looks plausible" can still be wrong (off-by-one rank, max instead of sum).
"""
import numpy as np
import pytest

# eval/ is dev tooling and is excluded from the API image by .dockerignore, so inside
# `docker compose exec api pytest` this whole file is skipped (same pattern as the compose-file tests).
pytest.importorskip("eval.run", reason="eval/ is not copied into the API image; run these on the host")

from eval.run import RRF_K, evaluate, rrf_order  # noqa: E402


def test_rrf_uses_one_over_k_plus_rank_summed_across_lists():
    # Ranks are 1-based. With k=60:
    #   chunk 0: model rank 1                  -> 1/61                = 0.016393
    #   chunk 1: model rank 2 + bm25 rank 2    -> 1/62 + 1/62         = 0.032258
    #   chunk 2: model rank 3 + bm25 rank 1    -> 1/63 + 1/61         = 0.032266
    # So chunk 2 beats chunk 1 by a hair: only the exact formula gives [2, 1, 0].
    assert rrf_order([0, 1, 2], [2, 1], rrf_k=60) == [2, 1, 0]


def test_ranks_start_at_one_not_zero():
    # A small constant makes the off-by-one visible (with k=60 it hides in the 4th decimal).
    # model=[0,1,2], bm25=[3,2], k=1, ranks counted from 1:
    #   c0 1/2 = .500   c1 1/3 = .333   c2 1/4 + 1/3 = .583   c3 1/2 = .500
    # -> c2 first; c0 and c3 tie and the model's order puts c0 ahead; c1 last: [2, 0, 3, 1].
    # (Counted from 0 it would be [0, 3, 2, 1].)
    assert rrf_order([0, 1, 2], [3, 2], rrf_k=1) == [2, 0, 3, 1]


def test_a_chunk_found_by_only_one_method_still_appears():
    assert set(rrf_order([0, 1, 2], [5])) == {0, 1, 2, 5}


def test_empty_keyword_leg_leaves_the_vector_order_unchanged():
    assert rrf_order([3, 1, 2, 0], []) == [3, 1, 2, 0]


def test_exact_ties_are_broken_by_the_models_order():
    # Both chunks have identical fused scores (1/61 + 1/62); the model's order decides.
    assert rrf_order([0, 1], [1, 0]) == [0, 1]
    assert rrf_order([1, 0], [0, 1]) == [1, 0]


def test_agreement_beats_a_single_top_rank():
    # Chunk 9 is #1 for the keyword leg only; chunk 4 is #2 in BOTH lists. Agreement wins.
    assert rrf_order([7, 4, 8], [9, 4])[0] == 4


def test_default_constant_is_the_standard_60():
    assert RRF_K == 60


def _fake_evaluate(texts, question, anchors):
    chunks = [{"file": f"f{i}.md", "i": 0, "text": t} for i, t in enumerate(texts)]
    # The "model" puts chunk 2 first, then 0, then 1 (a fixed, made-up preference).
    docs = np.array([[0.5, 0.0], [0.1, 0.0], [0.9, 0.0]], dtype=np.float32)
    gold = [{"id": "Q1", "category": "code", "answerable": True, "question": question, "anchors": anchors}]
    return evaluate(lambda _t: docs, lambda _q: np.array([1.0, 0.0], dtype=np.float32), chunks, gold, k=3)


def test_a_chunk_sharing_no_word_with_the_question_gets_no_keyword_rank():
    # Only chunk 0 contains "widget". Chunks 1 and 2 have a BM25 score of exactly 0, which is
    # "not matched", not "ranked last": a database full-text search would not return them.
    rows, _, _, rankings = _fake_evaluate(
        ["the widget service", "unrelated text here", "more unrelated words"], "widget", ["widget service"])
    assert rankings["Q1"]["bm25"] == ["f0.md#0"]
    # Vector order is 2, 0, 1; fused: chunk 0 is in both lists so it moves to the top.
    # Whole fused list, worked out by hand with k=60:
    #   chunk 0: model rank 2 + bm25 rank 1 = 1/62 + 1/61 = .0325
    #   chunk 2: model rank 1 only          = 1/61        = .0164
    #   chunk 1: model rank 3 only          = 1/63        = .0159
    assert rankings["Q1"]["hybrid"] == ["f0.md#0", "f2.md#0", "f1.md#0"]
    assert rows[0]["hybrid_rank"] == 1


def test_rankings_are_saved_per_question_for_later_rescoring():
    _, _, _, rankings = _fake_evaluate(["alpha beta", "gamma", "delta"], "alpha", ["alpha beta"])
    assert set(rankings["Q1"]) == {"model", "bm25", "hybrid"}
    assert rankings["Q1"]["model"] == ["f2.md#0", "f0.md#0", "f1.md#0"]


def test_rescoring_saved_rankings_reproduces_the_live_hybrid_ranks():
    """eval.rescore must agree with the run that produced the file (same chunks, same constant)."""
    from eval.rescore import rescore

    texts = ["the widget service", "unrelated text here", "more unrelated words"]
    chunks = [{"file": f"f{i}.md", "i": 0, "text": t} for i, t in enumerate(texts)]
    rows, _, _, rankings = _fake_evaluate(texts, "widget", ["widget service"])
    gold = [{"id": "Q1", "category": "code", "answerable": True, "question": "widget", "anchors": ["widget service"]}]
    redone = rescore({"rankings": rankings}, chunks, gold, k=3, rrf_k=60)
    assert redone[0]["hybrid_rank"] == rows[0]["hybrid_rank"] == 1
    assert redone[0]["rank"] == rows[0]["rank"] and redone[0]["bm25_rank"] == rows[0]["bm25_rank"]
    # A different constant is accepted and still returns a rank for the question.
    assert rescore({"rankings": rankings}, chunks, gold, k=3, rrf_k=5)[0]["hybrid_rank"] is not None
