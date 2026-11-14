"""
Batch D, Round 3: the pre-agreed decision rule in eval/decide.py. Pure logic, no database.
The v2 fixture is the real per-question table from the nomic run of 2026-11-08 (BM25 rank, model rank,
hybrid rank of the first correct chunk), so these tests also pin the numbers already verified by hand.
"""
import pytest

# eval/ is excluded from the API image by .dockerignore: skip the whole file there (run it on the host).
pytest.importorskip("eval.decide", reason="eval/ is not copied into the API image; run these on the host")

from eval.decide import decide  # noqa: E402

# (id, category, bm25_rank, model_rank, hybrid_rank)
V2 = [
    ("P1", "paraphrase", 1, 1, 1),
    ("P2", "paraphrase", 17, 1, 4),
    ("P3", "paraphrase", 1, 2, 1),
    ("P4", "paraphrase", 7, 25, 14),
    ("P5", "paraphrase", 14, 36, 19),
    ("P6", "paraphrase", 1, 2, 1),
    ("P7", "paraphrase", 18, 17, 6),
    ("P8", "paraphrase", 7, 13, 4),
    ("P9", "paraphrase", 6, 2, 1),
    ("P10", "paraphrase", 1, 2, 2),
    ("I1", "identifier", 7, 1, 2),
    ("I2", "identifier", 1, 1, 1),
    ("I3", "identifier", 2, 5, 3),
    ("I4", "identifier", 2, 1, 1),
    ("C1", "code", 2, 1, 1),
    ("C2", "code", 94, 55, 82),
    ("C3", "code", 1, 4, 3),
    ("C4", "code", 8, 26, 10),
    ("Y1", "broad", 16, 4, 2),
    ("Y2", "broad", 33, 2, 7),
    ("Y3", "broad", 12, 36, 17),
    ("Y4", "broad", 47, 6, 14),
    ("Y5", "code", 11, 1, 2),
    ("Y8", "broad", 2, 1, 1),
    ("Y9", "code", 1, 2, 1),
    ("Y10", "code", 40, 1, 7),
    ("Y11", "code", 3, 3, 2),
    ("Y12", "code", 8, 5, 11),
]


def rows(table):
    return [dict(id=i, category=c, answerable=True, bm25_rank=b, rank=m, hybrid_rank=h) for i, c, b, m, h in table]


def cond(result, n):
    return result["conditions"][n - 1]


def test_v2_numbers_match_the_ones_verified_by_hand():
    res = decide(rows(V2), [])
    detail = cond(res, 1)[2]
    assert "k=5: BM25 12, model 20, hybrid 18 of 28" in detail[0]
    assert "k=10: BM25 18, model 21, hybrid 22 of 28" in detail[1]
    assert "MRR BM25 0.382, model 0.489, hybrid 0.485" in cond(res, 2)[2][0]


def test_on_the_real_v2_data_hybrid_does_not_pass():
    res = decide(rows(V2), rows(V2))
    assert res["ship"] is False
    assert cond(res, 1)[1] is False      # -2 questions at k=5 and +1 at k=10, far below +5 points
    assert cond(res, 2)[1] is False      # 0.485 < 0.489, strict on purpose


def _winning(prefix, n=10):
    # Hybrid finds everything at rank 1; BM25 and the model each find only some, at rank 3.
    t = []
    for j in range(n):
        t.append((f"{prefix}{j}", "paraphrase", 1 if j < 3 else 50, 1 if j in (0, 1, 5) else 50, 1))
    return rows(t)


def test_a_clear_win_everywhere_ships():
    res = decide(_winning("a") + _winning("b"), _winning("b"))
    assert [c[1] for c in res["conditions"]] == [True, True, True, True]
    assert res["ship"] is True


def test_recall_gap_of_exactly_five_points_passes_and_just_under_fails():
    # 20 answerable questions: 1 question = 5.0 points. Hybrid finds 1 more than the better method at both k.
    base = [(f"q{j}", "code", 1, 1, 1) for j in range(10)] + [(f"r{j}", "code", 50, 50, 50) for j in range(9)]
    plus_one = base + [("x", "code", 50, 50, 1)]
    assert cond(decide(rows(plus_one), rows(plus_one)), 1)[1] is True          # +1 of 20 = +5.0 points
    plus_one_of_21 = plus_one + [("y", "code", 50, 50, 50)]                    # +1 of 21 = +4.8 points
    assert cond(decide(rows(plus_one_of_21), rows(plus_one_of_21)), 1)[1] is False


def test_k5_alone_is_enough_to_fail_the_recall_condition():
    # Hybrid wins big at k=10 but not at k=5 (the app's default).
    t = [(f"q{j}", "code", 50, 7, 7) for j in range(5)] + [(f"w{j}", "code", 50, 50, 8) for j in range(5)]
    res = decide(rows(t), rows(t))
    assert cond(res, 1)[1] is False
    assert res["ship"] is False


def test_one_category_losing_two_questions_to_the_model_blocks_shipping():
    good = [(f"g{j}", "paraphrase", 50, 50, 1) for j in range(10)]
    hurt = [("h0", "broad", 50, 1, 50), ("h1", "broad", 50, 1, 50), ("h2", "broad", 50, 1, 50)]
    res = decide(rows(good + hurt), rows(good))
    assert cond(res, 3)[1] is False
    assert "harmed" in "".join(cond(res, 3)[2])


def test_one_lost_question_in_a_category_is_tolerated():
    good = [(f"g{j}", "paraphrase", 50, 50, 1) for j in range(10)]
    one = [("h0", "broad", 50, 1, 50), ("h1", "broad", 50, 1, 1)]
    assert cond(decide(rows(good + one), rows(good)), 3)[1] is True


def test_holdout_must_agree_even_if_the_combined_numbers_win():
    v3_bad = [(f"v{j}", "paraphrase", 1, 1, 1) for j in range(5)]    # hybrid ties the others: no gain
    v2_great = [(f"a{j}", "paraphrase", 50, 50, 1) for j in range(20)]
    res = decide(rows(v2_great + v3_bad), rows(v3_bad))
    assert cond(res, 1)[1] is True and cond(res, 4)[1] is False and res["ship"] is False


def test_unanswerable_questions_are_ignored():
    t = rows(V2) + [dict(id="U1", category="unanswerable", answerable=False, bm25_rank=None, rank=None, hybrid_rank=None)]
    assert "of 28" in cond(decide(t, []), 1)[2][0]


def test_a_category_losing_exactly_two_questions_is_blocked():
    # The tolerance is ONE question: losing two must fail (guards against a loosened tolerance).
    t = [("h0", "broad", 50, 1, 50), ("h1", "broad", 50, 1, 50)]      # model finds both, hybrid neither
    assert cond(decide(rows(t), rows(t)), 3)[1] is False


def test_no_harm_is_measured_against_the_model_alone_not_the_better_single_method():
    # Here BM25 beats the model in this category, and hybrid only matches the model. The app today is the
    # model alone, so hybrid is not "harming" anything: condition 3 must pass.
    t = [(f"c{j}", "code", 1, 50, 50) for j in range(4)]
    assert cond(decide(rows(t), rows(t)), 3)[1] is True
