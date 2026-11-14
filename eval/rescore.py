"""
Re-score a saved evaluation run with a different fusion constant, WITHOUT re-embedding.

    python -m eval.rescore eval/results/<file>.json                 # same k and RRF constant as the run
    python -m eval.rescore eval/results/<file>.json --k 5 --rrf-k 20

Why this exists: a result file used to hold only the rank of the FIRST correct chunk per question,
which is not enough to try a different way of combining the two methods. Runs made after Batch D
Round 1 also save each method's full ranking ("rankings"), so fusion variants cost seconds, not
the ~4 minutes the real model needs to embed the corpus.

Read this before using it: trying many constants on 28 questions and keeping the best one is
tuning on the test. Fix the constant BEFORE looking (the default, 60, is the published standard),
and treat any other value as a sensitivity check, not a result.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from eval.check_golden import hit, load_chunks  # noqa: E402
from eval.run import RRF_K, first_hit_rank, hybrid_report, rrf_order  # noqa: E402


def rescore(result: dict, chunks: list, gold: list, k: int, rrf_k: int) -> list:
    """Rebuild the per-question rows (BM25, model, hybrid ranks) from saved rankings."""
    index = {f"{c['file']}#{c['i']}": n for n, c in enumerate(chunks)}
    rows = []
    for q in gold:
        if not q["answerable"]:
            continue
        saved = result["rankings"].get(q["id"])
        if saved is None:
            sys.exit(f"{q['id']} is not in this result file's rankings (was the golden set changed since the run?)")
        model = [index[x] for x in saved["model"]]
        bm25 = [index[x] for x in saved["bm25"]]
        rows.append(dict(id=q["id"], category=q["category"], answerable=True,
                         rank=first_hit_rank(model, chunks, q["anchors"]),
                         bm25_rank=first_hit_rank(bm25, chunks, q["anchors"]),
                         hybrid_rank=first_hit_rank(rrf_order(model, bm25, rrf_k), chunks, q["anchors"])))
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("result", help="a JSON file from eval/results/ that contains 'rankings'")
    ap.add_argument("--k", type=int, default=None, help="recall@k (default: the run's own k)")
    ap.add_argument("--rrf-k", type=int, default=RRF_K)
    ap.add_argument("--golden", default="golden_set.json", help="golden set file inside eval/ (default: v2)")
    a = ap.parse_args()

    result = json.loads(Path(a.result).read_text(encoding="utf-8"))
    if "rankings" not in result:
        sys.exit("This result file has no 'rankings' (it predates Batch D Round 1). Re-run eval.run once to create one.")
    gold = json.loads((ROOT / a.golden).read_text(encoding="utf-8"))["questions"]
    k = a.k or result["k"]
    hybrid_report(rescore(result, load_chunks(), gold, k, a.rrf_k), k, a.rrf_k)


if __name__ == "__main__":
    main()
