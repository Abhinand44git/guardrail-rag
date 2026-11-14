"""
Batch D, Round 5: the pre-agreed chunking decision rule in eval/decide_chunking.py (pure logic, no database).
"""
import pytest

pytest.importorskip("eval.decide_chunking", reason="eval/ is not copied into the API image; run these on the host")

from eval.decide_chunking import compare, mean_top_words, pick  # noqa: E402


def rows(table):
    # (id, category, nomic rank)
    return [dict(id=i, category=c, answerable=True, rank=r) for i, c, r in table]


def run(base, var, v2n, base_ctx=100.0, var_ctx=100.0):
    """base/var: lists of ranks; the first v2n questions are 'v2', the rest 'v3'. All in category 'code'."""
    ids = [f"q{j}" for j in range(len(base))]
    b = rows([(i, "code", r) for i, r in zip(ids, base)])
    v = rows([(i, "code", r) for i, r in zip(ids, var)])
    return compare(b, v, set(ids[:v2n]), set(ids[v2n:]), base_ctx, var_ctx)


def cond(res, n):
    return res["conditions"][n - 1]


def test_a_clear_win_that_both_sets_agree_on_is_adoptable():
    base = [1] * 10 + [50] * 10
    var = [1] * 10 + [1, 1] + [50] * 8           # two more found, one in v2 and one in v3? (see ids below)
    res = run(base, var, v2n=10)
    assert [c[1] for c in res["conditions"]] == [True] * 5 and res["adoptable"] is True


def test_exactly_five_points_passes_and_just_under_fails():
    base = [1] * 10 + [50] * 10
    var = [1] * 11 + [50] * 9                     # +1 of 20 = +5.0 points at both depths (q10 is in v3)
    assert cond(run(base, var, v2n=10), 1)[1] is True
    base21, var21 = base + [50], var + [50]       # +1 of 21 = +4.8
    assert cond(run(base21, var21, v2n=10), 1)[1] is False


def test_a_gain_at_k10_only_is_not_enough():
    base = [50] * 10 + [50] * 10
    var = [8] * 2 + [50] * 18                     # found only from rank 8: counts at k=10, not at k=5
    assert cond(run(base, var, v2n=10), 1)[1] is False


def test_lower_mrr_blocks_adoption_even_with_more_questions_found():
    base = [1] * 10 + [50] * 10
    var = [2] * 10 + [4] * 2 + [50] * 8           # more found, but the top answers slipped from rank 1 to 2
    res = run(base, var, v2n=10)
    assert cond(res, 2)[1] is False and res["adoptable"] is False


def _two_categories(base_a, var_a, base_b, var_b):
    t_b = [(f"a{i}", "paraphrase", r) for i, r in enumerate(base_a)] + [(f"b{i}", "code", r) for i, r in enumerate(base_b)]
    t_v = [(f"a{i}", "paraphrase", r) for i, r in enumerate(var_a)] + [(f"b{i}", "code", r) for i, r in enumerate(var_b)]
    ids = [x[0] for x in t_b]
    return compare(rows(t_b), rows(t_v), set(ids[::2]), set(ids[1::2]), 100.0, 100.0)


def test_a_category_losing_two_questions_blocks_adoption_and_losing_one_is_tolerated():
    big_win = [50] * 10
    hurt2 = _two_categories([50] * 10, [1] * 10, [1, 1, 1], [50, 50, 1])
    hurt1 = _two_categories([50] * 10, [1] * 10, [1, 1, 1], [50, 1, 1])
    assert cond(hurt2, 3)[1] is False and cond(hurt1, 3)[1] is True


def test_v3_contradicting_the_combined_result_blocks_adoption():
    # Combined gain is large, but on v3 alone the variant finds fewer than today's.
    base = [50] * 10 + [1, 1, 1] + [50] * 7
    var = [1] * 10 + [50, 50, 50] + [50] * 7      # v2: +10; v3: -3
    res = run(base, var, v2n=10)
    assert cond(res, 4)[1] is False and "contradicts" in "".join(cond(res, 4)[2])


def test_context_guard_exactly_half_passes_and_below_fails():
    base = [1] * 10 + [50] * 10
    var = [1] * 11 + [50] * 9
    assert cond(run(base, var, 10, base_ctx=100.0, var_ctx=50.0), 5)[1] is True
    assert cond(run(base, var, 10, base_ctx=100.0, var_ctx=49.0), 5)[1] is False


def test_the_simplest_adoptable_variant_wins():
    ok = dict(adoptable=True, conditions=[])
    no = dict(adoptable=False, conditions=[])
    assert pick({"A_header": no, "B_structure": ok, "C_structure_header": ok}) == "B_structure"
    assert pick({"C_structure_header": ok, "A_header": ok}) == "A_header"
    assert pick({"A_header": no, "B_structure": no}) is None


def test_a_variant_run_that_misses_questions_is_refused():
    b = rows([("q1", "code", 1), ("q2", "code", 1)])
    with pytest.raises(ValueError):
        compare(b, rows([("q1", "code", 1)]), {"q1"}, {"q2"}, 1.0, 1.0)


def test_mean_top_words_uses_the_models_top_five_chunks():
    chunks = [dict(file="f", i=i, text=" ".join(["w"] * (10 * (i + 1)))) for i in range(7)]   # 10, 20, ... 70 words
    result = dict(rows=[dict(id="q1", answerable=True)], rankings=dict(q1=dict(model=[f"f#{i}" for i in (6, 5, 4, 3, 2, 1, 0)])))
    assert mean_top_words(result, chunks, n=5) == (70 + 60 + 50 + 40 + 30)


def test_mrr_is_strict_even_for_a_tiny_slip():
    # One top answer moves from rank 1 to rank 2: MRR falls by only 0.025, recall is unchanged. Strict means "lower".
    base = [1] * 10 + [50] * 10
    var = [2] + [1] * 9 + [50] * 10
    assert cond(run(base, var, v2n=10), 2)[1] is False
