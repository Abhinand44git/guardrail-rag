"""
Step 1 of the eval: sanity-check the golden set WITHOUT any embedding model.
  (a) every anchor really exists in the chunked corpus (a typo'd anchor would
      silently score every model 0 on that question)
  (b) a plain keyword search (BM25) baseline, so we can see which questions
      are "too easy" (keywords alone solve them) and which need real semantics.
Run from the project root:  python -m eval.check_golden
"""
import json, math, re, sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from app.chunking import chunk_text  # same chunker the real app uses

K = 5

def norm(s): return re.sub(r"\s+", " ", s).lower()

def load_chunks():
    chunks = []
    for p in sorted((ROOT / "corpus").rglob("*")):
        if not p.is_file(): continue
        st = "markdown" if p.suffix == ".md" else "code"
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, c in enumerate(chunk_text(text, st)):
            chunks.append({"file": str(p.relative_to(ROOT / "corpus")), "i": i, "text": c})
    return chunks

tok = lambda s: re.findall(r"[a-z0-9_]+", s.lower())

class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.tf = [Counter(tok(d)) for d in docs]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = sum(self.len) / len(docs)
        df = Counter(w for t in self.tf for w in t)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - c + .5) / (c + .5)) for w, c in df.items()}
        self.k1, self.b = k1, b
    def scores(self, q):
        out = []
        for tf, L in zip(self.tf, self.len):
            s = 0.0
            for w in tok(q):
                if w in tf:
                    f = tf[w]
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * L / self.avg))
            out.append(s)
        return out

def hit(chunk, anchors):
    t = norm(chunk["text"])
    return any(norm(a) in t for a in anchors)

def main():
    gold = json.loads((ROOT / "golden_set.json").read_text(encoding="utf-8"))["questions"]
    chunks = load_chunks()
    print(f"corpus: {len(chunks)} chunks from {len({c['file'] for c in chunks})} files\n")

    bad = 0
    for q in gold:
        if q["answerable"]:
            n = sum(hit(c, q["anchors"]) for c in chunks)
            if n == 0:
                bad += 1; print(f"  ANCHOR NOT FOUND  {q['id']}: {q['anchors']}")
    print("anchor check:", "all anchors found" if not bad else f"{bad} missing")

    bm = BM25([c["text"] for c in chunks])
    per = defaultdict(list)
    print(f"\n{'id':4} {'cat':12} rank-of-first-hit (BM25, top {K} counts)")
    for q in gold:
        sc = bm.scores(q["question"])
        order = sorted(range(len(chunks)), key=lambda i: -sc[i])
        if not q["answerable"]:
            print(f"{q['id']:4} {q['category']:12} (unanswerable) top score {sc[order[0]]:.2f} in {chunks[order[0]]['file']}")
            continue
        rank = next((r for r, i in enumerate(order, 1) if hit(chunks[i], q["anchors"])), None)
        per[q["category"]].append(rank)
        print(f"{q['id']:4} {q['category']:12} {rank if rank else 'miss'}  ({'HIT' if rank and rank <= K else 'no hit'}@{K})")

    print(f"\nBM25 baseline by category (recall@{K}, MRR):")
    allr = []
    for cat, ranks in per.items():
        allr += ranks
        r = sum(1 for x in ranks if x and x <= K) / len(ranks)
        mrr = sum(1 / x for x in ranks if x) / len(ranks)
        print(f"  {cat:12} n={len(ranks):2}  recall@{K}={r:.0%}  MRR={mrr:.2f}")
    r = sum(1 for x in allr if x and x <= K) / len(allr)
    print(f"  {'ALL':12} n={len(allr):2}  recall@{K}={r:.0%}  MRR={sum(1/x for x in allr if x)/len(allr):.2f}")

if __name__ == "__main__":
    main()
