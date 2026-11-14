"""
Batch D, Round 3: apply the PRE-AGREED rule for "should hybrid search be built?" -- mechanically.

    python -m eval.decide <result on golden_set.json> <result on golden_set_v3.json>

The rule was fixed BEFORE the v3 results existed (owner chose the "combined rule"). This script exists
so the verdict cannot be re-interpreted after the numbers are seen. Hybrid ships ONLY if ALL FOUR hold,
measured on answerable questions, "better single method" = whichever of BM25 / nomic finds more:

  1. RECALL:   on v2 + v3 combined, hybrid beats the better single method by >= 5.0 points at BOTH k=5 and k=10
               (k=5 is the app's default).
  2. RANKING:  combined MRR of hybrid is not lower than the better single method's (strict, no tolerance:
               chosen conservatively so a close call means "do not build").
  3. NO HARM:  in no category does hybrid find more than ONE fewer question than the model alone
               (the model alone is what the app does today), at k=5 or k=10.
  4. HOLD-OUT: on v3 alone (questions nobody had looked at when v2 was analysed) hybrid beats the better
               single method at both k=5 and k=10 (strictly more, any margin: "points the same way").

Anything else: write the result up and stop. A negative result is a legitimate outcome.
Caveat printed with the verdict: v3 questions with author='assistant' were not written blind.
"""
from __future__ import annotations

import json
import sys

KS = (5, 10)
MIN_GAP_POINTS = 5.0
MAX_CATEGORY_LOSS = 1


def _found(rows, key, k):
    return sum(1 for r in rows if r[key] and r[key] <= k)


def _mrr(rows, key):
    return sum(1 / r[key] for r in rows if r[key]) / len(rows) if rows else 0.0


def decide(all_rows, v3_rows):
    """Both arguments: lists of per-question dicts with category, rank (model), bm25_rank, hybrid_rank.
    Unanswerable questions are ignored. Returns {'conditions': [...], 'ship': bool}."""
    allr = [r for r in all_rows if r.get("answerable", True)]
    v3 = [r for r in v3_rows if r.get("answerable", True)]
    n = len(allr)
    out = []

    # 1. recall gap on combined
    detail, ok = [], True
    for k in KS:
        b, m, h = (_found(allr, key, k) for key in ("bm25_rank", "rank", "hybrid_rank"))
        gap = 100 * (h - max(b, m)) / n
        detail.append(f"k={k}: BM25 {b}, model {m}, hybrid {h} of {n}  -> {gap:+.1f} points vs the better single method")
        ok &= gap >= MIN_GAP_POINTS
    out.append(("1 RECALL (>= +5.0 points at k=5 and k=10, combined)", ok, detail))

    # 2. MRR
    mb, mm, mh = (_mrr(allr, key) for key in ("bm25_rank", "rank", "hybrid_rank"))
    out.append(("2 RANKING (hybrid MRR >= better single method's)", mh >= max(mb, mm),
                [f"MRR BM25 {mb:.3f}, model {mm:.3f}, hybrid {mh:.3f}"]))

    # 3. no category harmed vs the model alone
    detail, ok = [], True
    for cat in sorted({r["category"] for r in allr}):
        sel = [r for r in allr if r["category"] == cat]
        for k in KS:
            m, h = _found(sel, "rank", k), _found(sel, "hybrid_rank", k)
            bad = h < m - MAX_CATEGORY_LOSS
            ok &= not bad
            detail.append(f"{cat:11} k={k:2}: model {m}, hybrid {h} of {len(sel)}" + ("   <-- harmed" if bad else ""))
    out.append(("3 NO HARM (no category loses more than 1 question vs the model alone)", ok, detail))

    # 4. hold-out v3 alone
    detail, ok = [], True
    for k in KS:
        b, m, h = (_found(v3, key, k) for key in ("bm25_rank", "rank", "hybrid_rank"))
        detail.append(f"k={k}: BM25 {b}, model {m}, hybrid {h} of {len(v3)}")
        ok &= h > max(b, m)
    out.append(("4 HOLD-OUT (v3 alone: hybrid strictly beats the better single method at k=5 and k=10)", ok, detail))
    return dict(conditions=out, ship=all(c[1] for c in out))


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    v2 = json.load(open(sys.argv[1], encoding="utf-8"))["rows"]
    v3 = json.load(open(sys.argv[2], encoding="utf-8"))["rows"]
    res = decide(v2 + v3, v3)
    print("\nRound 3 rule (agreed before the v3 results existed)\n")
    for name, ok, detail in res["conditions"]:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        for d in detail:
            print("         " + d)
    print("\nVERDICT:", "all four hold -> hybrid may be built (Round 4)." if res["ship"]
          else "at least one condition fails -> do NOT build hybrid; write up the result and stop.")
    print("(Caveat: v3 questions with author='assistant' were written with the corpus visible, not blind.)")


if __name__ == "__main__":
    main()
