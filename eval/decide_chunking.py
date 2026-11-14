"""
Batch D, Round 5: apply the PRE-AGREED rule for "should the app change how it chunks?" -- mechanically.

    python -m eval.decide_chunking <current result on golden_set.json> <current result on golden_set_v3.json> \\
                                   <variant result> [<variant result> ...]

Each variant result is ONE run of  python -m eval.run ... --chunking <variant> --golden golden_set.json,golden_set_v3.json
The baseline is today's chunking ("current"), from the two existing result files. Only the embedding model's
ranking is compared (this round is about chunks, not about hybrid search, which ADR-0027 closed).

A variant is ADOPTABLE only if ALL FIVE hold, on answerable questions (v2 + v3 = 53):
  1. RECALL      beats today's chunking by >= 5.0 points at BOTH k=5 and k=10.
  2. RANKING     model MRR is not lower than today's (strict, no tolerance).
  3. NO HARM     no category finds more than ONE fewer question than today's, at k=5 or k=10.
  4. CONSISTENCY on v2 alone AND on v3 alone the variant finds at least as many as today's, at k=5 and k=10
                 (neither question set may contradict the combined result).
  5. CONTEXT     the average amount of text in the top-5 chunks (words) is at least 50% of today's, because
                 smaller chunks hand the caller less to work with.
If several variants are adoptable, the SIMPLEST wins (A_header, then B_structure, then C_structure_header).
If none is adoptable, the app keeps today's chunking and the result is written up. A "no" is a valid outcome.

Caveats printed with the verdict: three variants were tried on questions that were already used for the hybrid
decision (some of v3 was written with the corpus visible); passing needs agreement on two separate sets and at
least +5 points at both depths, but a pass is still evidence, not proof.
"""
from __future__ import annotations

import json
import sys

from eval.chunkers import SIMPLICITY_ORDER, load_chunks_variant

KS = (5, 10)
MIN_GAP_POINTS = 5.0
MAX_CATEGORY_LOSS = 1
MIN_CONTEXT_RATIO = 0.5


def _found(rows, k):
    return sum(1 for r in rows if r["rank"] and r["rank"] <= k)


def _mrr(rows):
    return sum(1 / r["rank"] for r in rows if r["rank"]) / len(rows) if rows else 0.0


def compare(base_rows, var_rows, v2_ids, v3_ids, base_ctx, var_ctx):
    """base_rows/var_rows: per-question dicts (id, category, answerable, rank = nomic's first-correct-chunk rank).
    base_ctx/var_ctx: average words in the top-5 chunks. Returns {'conditions': [...], 'adoptable': bool}."""
    b = {r["id"]: r for r in base_rows if r.get("answerable", True)}
    v = {r["id"]: r for r in var_rows if r.get("answerable", True) and r["id"] in b}
    if set(v) != set(b):
        raise ValueError("the variant run does not cover the same questions as the baseline")
    ids = sorted(b)
    B, V = [b[i] for i in ids], [v[i] for i in ids]
    n = len(ids)
    out = []

    detail, ok = [], True
    for k in KS:
        bf, vf = _found(B, k), _found(V, k)
        gap = 100 * (vf - bf) / n
        detail.append(f"k={k}: today's {bf}, variant {vf} of {n}  -> {gap:+.1f} points")
        ok &= gap >= MIN_GAP_POINTS
    out.append(("1 RECALL (>= +5.0 points at k=5 and k=10)", ok, detail))

    mb, mv = _mrr(B), _mrr(V)
    out.append(("2 RANKING (MRR not lower)", mv >= mb, [f"MRR today's {mb:.3f}, variant {mv:.3f}"]))

    detail, ok = [], True
    for cat in sorted({r["category"] for r in B}):
        for k in KS:
            bf = _found([r for r in B if r["category"] == cat], k)
            vf = _found([r for r in V if r["category"] == cat], k)
            bad = vf < bf - MAX_CATEGORY_LOSS
            ok &= not bad
            detail.append(f"{cat:11} k={k:2}: today's {bf}, variant {vf}" + ("   <-- harmed" if bad else ""))
    out.append(("3 NO HARM (no category loses more than 1 question)", ok, detail))

    detail, ok = [], True
    for name, subset in (("v2 alone", v2_ids), ("v3 alone", v3_ids)):
        Bs, Vs = [b[i] for i in ids if i in subset], [v[i] for i in ids if i in subset]
        for k in KS:
            bf, vf = _found(Bs, k), _found(Vs, k)
            ok &= vf >= bf
            detail.append(f"{name:8} k={k:2}: today's {bf}, variant {vf} of {len(Bs)}" + ("   <-- contradicts" if vf < bf else ""))
    out.append(("4 CONSISTENCY (neither v2 nor v3 alone is worse)", ok, detail))

    ratio = var_ctx / base_ctx if base_ctx else 0.0
    out.append(("5 CONTEXT (top-5 text volume >= 50% of today's)", ratio >= MIN_CONTEXT_RATIO,
                [f"average words in the top-5 chunks: today's {base_ctx:.0f}, variant {var_ctx:.0f}  ({ratio:.0%})"]))
    return dict(conditions=out, adoptable=all(c[1] for c in out))


def pick(results):
    """results: {variant name: compare() output}. The simplest adoptable variant, or None."""
    for name in SIMPLICITY_ORDER:
        if name in results and results[name]["adoptable"]:
            return name
    return None


def mean_top_words(result, chunks, n=5):
    """Average words in the model's top-n chunks, over the answerable questions of a saved run."""
    size = {f"{c['file']}#{c['i']}": len(c["text"].split()) for c in chunks}
    totals = [sum(size[x] for x in result["rankings"][r["id"]]["model"][:n])
              for r in result["rows"] if r.get("answerable", True)]
    return sum(totals) / len(totals)


def _load(path):
    return json.load(open(path, encoding="utf-8"))


def main():
    if len(sys.argv) < 4:
        sys.exit(__doc__)
    v2, v3 = _load(sys.argv[1]), _load(sys.argv[2])
    for r in (v2, v3):
        if r.get("chunking", "current") != "current":
            sys.exit("the first two files must be runs with today's chunking (--chunking current)")
    base_rows = v2["rows"] + v3["rows"]
    v2_ids = {r["id"] for r in v2["rows"]}
    v3_ids = {r["id"] for r in v3["rows"]}
    cur = load_chunks_variant("current")
    base_ctx = (mean_top_words(v2, cur) * sum(r.get("answerable", True) for r in v2["rows"])
                + mean_top_words(v3, cur) * sum(r.get("answerable", True) for r in v3["rows"])) \
        / sum(r.get("answerable", True) for r in base_rows)
    results = {}
    for path in sys.argv[3:]:
        run = _load(path)
        name = run.get("chunking", "current")
        if name == "current":
            sys.exit(f"{path} was run with today's chunking; pass the variant runs here")
        results[name] = compare(base_rows, run["rows"], v2_ids, v3_ids, base_ctx,
                                mean_top_words(run, load_chunks_variant(name)))
    print("\nRound 5 rule (agreed before any chunking result existed)\n")
    for name in SIMPLICITY_ORDER:
        if name not in results:
            continue
        print(f"=== {name}: {'ADOPTABLE' if results[name]['adoptable'] else 'not adoptable'}")
        for cname, ok, detail in results[name]["conditions"]:
            print(f"  [{'PASS' if ok else 'FAIL'}] {cname}")
            for d in detail:
                print("         " + d)
        print()
    winner = pick(results)
    print("VERDICT:", f"adopt {winner} (the simplest variant that passes all five)." if winner
          else "no variant passes all five -> keep today's chunking; write up the result and stop.")
    print("(Caveat: v3 was partly written with the corpus visible, and these questions were already used for the hybrid decision.)")


if __name__ == "__main__":
    main()
