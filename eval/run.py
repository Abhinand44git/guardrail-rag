"""
Step 2 of the eval: score an embedding model on the golden set.

Run from the project root, inside the eval virtual environment:

    python -m eval.run --model nomic-ai/nomic-embed-text-v1.5
    python -m eval.run --model BAAI/bge-small-en-v1.5
    python -m eval.run --model fake-hash          # no download; checks the harness itself

What it does, in plain terms:
  1. Cuts the corpus into chunks with the SAME chunker the app uses.
  2. Turns every chunk into a vector with the model, and every question too.
  3. For each question, ranks all chunks by cosine similarity (1.0 = same
     direction = "same meaning"), and finds where the first chunk containing
     an anchor phrase landed (its "rank").
  4. Prints recall@k / MRR next to the keyword (BM25) baseline, looks at the
     unanswerable questions, and times everything.
  5. Saves a JSON record (versions, revision, numbers) to eval/results/ so the
     result can be cited later as evidence in an ADR.

Hybrid column (Batch D, Round 1): every run also fuses the two rankings with Reciprocal Rank
Fusion (RRF) and reports it next to BM25 and the model, plus which questions flip. Offline only:
the app does not do this. The full rankings (RANKED_TOP_N, more than the corpus holds) of both methods are saved in the result
JSON ("rankings"), so fusion variants can be re-scored later WITHOUT re-embedding (~4 minutes).
Use  python -m eval.rescore <result.json>  for that.

Not done here, on purpose: the app zero-pads vectors to 1536 dims before
storing them. Zero-padding does not change cosine similarity, so the eval
skips it; it only matters for storage cost.

Width test (ADR-0014): add  --dims 768,512,256,128  to also score the same
model with its vectors cut down to those widths. The corpus and questions are
embedded ONCE; every width is cut from the same vectors, so this costs one run,
not four. Only models trained for truncation ("Matryoshka" models, e.g. nomic)
are allowed. A plain full-width control row is always included and must match
a run without --dims.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import statistics
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from eval.check_golden import BM25, K as DEFAULT_K, hit, load_chunks, norm  # noqa: E402
from eval.chunkers import VARIANTS as CHUNKINGS, load_chunks_variant  # noqa: E402

# ---------------------------------------------------------------------------
# Per-model settings. WHY this exists: embedding models are trained to expect
# specific text prefixes ("search_document: " etc). Use the wrong ones and the
# model scores worse than it really is -- silently, with no error. So a model
# name alone is not enough to run a fair comparison.
#
# CAUTION: only nomic's prefixes are confirmed by this project's own docs.
# The others are from memory of their model cards -- open each model's
# Hugging Face page and check before you trust a comparison. Unknown models
# are refused unless you pass --doc-prefix and --query-prefix yourself.
# ---------------------------------------------------------------------------
MODEL_CONFIGS = {
    "nomic-ai/nomic-embed-text-v1.5": dict(
        doc_prefix="search_document: ", query_prefix="search_query: ",
        trust_remote_code=True, code_repo="nomic-ai/nomic-bert-2048", matryoshka=True),
    "BAAI/bge-small-en-v1.5": dict(
        doc_prefix="", query_prefix="Represent this sentence for searching relevant passages: ",
        trust_remote_code=False),
    "intfloat/e5-small-v2": dict(
        doc_prefix="passage: ", query_prefix="query: ", trust_remote_code=False),
    "sentence-transformers/all-MiniLM-L6-v2": dict(
        doc_prefix="", query_prefix="", trust_remote_code=False),
    # fake-hash is flagged only so the --dims plumbing can be tested without a download;
    # truncating a hashed bag of words means nothing about real models.
    "fake-hash": dict(doc_prefix="", query_prefix="", trust_remote_code=False, matryoshka=True),
}


class FakeHashModel:
    """A fake 'model' (hashed bag of words). Needs no download. It exists so
    the harness's own maths can be tested separately from any real model."""
    max_seq_length = None
    tokenizer = None

    def encode(self, texts, **_):
        out = np.zeros((len(texts), 512), dtype=np.float32)
        for i, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9_]+", t.lower()):
                out[i, zlib.crc32(w.encode()) % 512] += 1.0
        n = np.linalg.norm(out, axis=1, keepdims=True)
        n[n == 0] = 1.0
        return out / n


def resolve_snapshot(repo: str) -> str:
    """The exact commit of a repo that is in the local cache (what actually ran)."""
    try:
        from huggingface_hub import snapshot_download
        return Path(snapshot_download(repo, local_files_only=True)).name
    except Exception:
        return "unknown"


def versions() -> dict:
    out = {"python": platform.python_version(), "platform": platform.platform(),
           "processor": platform.processor()}
    for pkg in ("sentence-transformers", "transformers", "torch", "huggingface_hub", "numpy", "einops"):
        try:
            from importlib.metadata import version
            out[pkg] = version(pkg)
        except Exception:
            out[pkg] = "not installed"
    return out


def first_hit_rank(order, chunks, anchors):
    for r, i in enumerate(order, 1):
        if hit(chunks[i], anchors):
            return r
    return None


def parts_found(order, chunks, anchors, k):
    top = [norm(chunks[i]["text"]) for i in order[:k]]
    return sum(any(norm(a) in t for t in top) for a in anchors)


RRF_K = 60            # the constant from the original RRF paper; larger = flatter, smaller = top-heavy
RANKED_TOP_N = 200    # how much of each ranking is saved. The frozen corpus has 112 chunks, so this is the whole
                      # ranking, which makes eval.rescore exact (not an approximation from a truncated list)


def rrf_order(model_order, bm25_order, rrf_k=RRF_K):
    """Reciprocal Rank Fusion: score(chunk) = sum over lists of 1 / (rrf_k + rank in that list).
    Uses only ranks, so BM25 scores and cosine scores need no common scale.
    bm25_order must contain ONLY chunks with a keyword match (score > 0): a chunk that shares no
    word with the question is not 'ranked' by keyword search at all (a database full-text search
    would not return it either). Ties are broken by the model's rank."""
    score = {}
    for order in (model_order, bm25_order):
        for r, i in enumerate(order, 1):
            score[i] = score.get(i, 0.0) + 1.0 / (rrf_k + r)
    pos = {i: r for r, i in enumerate(model_order)}
    return sorted(score, key=lambda i: (-score[i], pos.get(i, len(pos))))


def metrics(ranks, k):
    if not ranks:
        return 0.0, 0.0
    return (sum(1 for r in ranks if r and r <= k) / len(ranks),
            sum(1 / r for r in ranks if r) / len(ranks))


def pct(values, p):
    s = sorted(values)
    return s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))]


def evaluate(embed_docs, embed_query, chunks, gold, k):
    """Core scoring. embed_docs(list[str]) -> matrix; embed_query(str) -> vector.
    Kept free of any model loading so it can be tested with stand-ins."""
    t0 = time.perf_counter()
    C = embed_docs([c.get("embed_text", c["text"]) for c in chunks])   # a header (variants A, C) is embedded but never matched against
    corpus_seconds = time.perf_counter() - t0

    bm = BM25([c["text"] for c in chunks])
    rows, q_ms, rankings = [], [], {}
    for q in gold:
        t0 = time.perf_counter()
        qv = embed_query(q["question"])
        q_ms.append((time.perf_counter() - t0) * 1000)
        scores = C @ qv
        order = list(np.argsort(-scores, kind="stable"))
        bsc = bm.scores(q["question"])
        border = sorted(range(len(chunks)), key=lambda i: -bsc[i])
        border_matched = [i for i in border if bsc[i] > 0]          # keyword leg of the fusion
        horder = rrf_order(order, border_matched)
        row = {"id": q["id"], "category": q["category"], "answerable": q["answerable"],
               "top1_score": float(scores[order[0]]), "top1_file": chunks[order[0]]["file"]}
        if q["answerable"]:
            row["rank"] = first_hit_rank(order, chunks, q["anchors"])
            row["bm25_rank"] = first_hit_rank(border, chunks, q["anchors"])
            row["hybrid_rank"] = first_hit_rank(horder, chunks, q["anchors"])
            if q.get("parts"):
                row["parts_total"] = len(q["anchors"])
                row["parts_model"] = parts_found(order, chunks, q["anchors"], k)
                row["parts_bm25"] = parts_found(border, chunks, q["anchors"], k)
                row["parts_hybrid"] = parts_found(horder, chunks, q["anchors"], k)
        rows.append(row)
        ids = lambda o: [f"{chunks[i]['file']}#{chunks[i]['i']}" for i in o[:RANKED_TOP_N]]
        rankings[q["id"]] = dict(model=ids(order), bm25=ids(border_matched), hybrid=ids(horder))
    return rows, corpus_seconds, q_ms, rankings


def report(rows, k):
    ans = [r for r in rows if r["answerable"]]
    print(f"\n{'id':4} {'category':12} {'BM25 rank':>9} {'model rank':>10} {'hybrid':>7} {'top1 cos':>9}  top-1 chunk's file")
    for r in rows:
        if r["answerable"]:
            print(f"{r['id']:4} {r['category']:12} {str(r['bm25_rank'] or 'miss'):>9} "
                  f"{str(r['rank'] or 'miss'):>10} {str(r['hybrid_rank'] or 'miss'):>7} {r['top1_score']:9.3f}  {r['top1_file']}")
        else:
            print(f"{r['id']:4} {r['category']:12} {'-':>9} {'-':>10} {'-':>7} {r['top1_score']:9.3f}  {r['top1_file']}   (unanswerable)")

    print(f"\nRecall@{k} and MRR   (BM25 keyword | this model | hybrid RRF)")
    cats = list(dict.fromkeys(r["category"] for r in ans)) + ["ALL"]
    summary = {}
    for cat in cats:
        sel = ans if cat == "ALL" else [r for r in ans if r["category"] == cat]
        br, bm_ = metrics([r["bm25_rank"] for r in sel], k)
        mr, mm = metrics([r["rank"] for r in sel], k)
        hr, hm = metrics([r["hybrid_rank"] for r in sel], k)
        summary[cat] = dict(n=len(sel), bm25_recall=br, bm25_mrr=bm_, model_recall=mr, model_mrr=mm,
                            hybrid_recall=hr, hybrid_mrr=hm)
        print(f"  {cat:12} n={len(sel):2}   recall {br:4.0%} | {mr:4.0%} | {hr:4.0%}    MRR {bm_:.2f} | {mm:.2f} | {hm:.2f}")
    print("  (one question = about "
          f"{100 / len(ans):.1f} points of recall; treat gaps smaller than ~10 points as noise)")

    parts = [r for r in ans if "parts_total" in r]
    if parts:
        print(f"\nMulti-part questions: pieces of the answer found in the top {k} chunks")
        for r in parts:
            print(f"  {r['id']:4} BM25 {r['parts_bm25']}/{r['parts_total']}   model {r['parts_model']}/{r['parts_total']}")

    un = [r for r in rows if not r["answerable"]]
    good = [r["top1_score"] for r in ans if r["rank"] == 1]
    if un and good:
        print("\nCan a similarity cutoff tell 'no answer' apart?")
        print(f"  answerable questions answered correctly at rank 1 ({len(good)}): "
              f"top-1 cosine min {min(good):.3f}, median {statistics.median(good):.3f}")
        print(f"  unanswerable questions: " + ", ".join(f"{r['id']} {r['top1_score']:.3f}" for r in un))
        if max(r["top1_score"] for r in un) < min(good):
            print("  -> every unanswerable score is below every correct answer: a cutoff would work on this set.")
        else:
            print("  -> some unanswerable scores reach into the correct-answer range: "
                  "a single cutoff would either let junk through or reject real answers.")
    return summary


# ---------------------------------------------------------------------------
# Width test helpers. Nomic's model card gives this exact recipe for shorter
# vectors: layer-norm the full vector, keep the first d numbers, then rescale
# to length 1. (Cutting without those steps would test something else.)
# ---------------------------------------------------------------------------
def _unit(x):
    x = np.asarray(x, dtype=np.float64)
    n = np.linalg.norm(x, axis=-1, keepdims=True)
    n = np.where(n == 0, 1.0, n)
    return (x / n).astype(np.float32)


def _layer_norm(x, eps=1e-5):
    x = np.asarray(x, dtype=np.float64)
    mu = x.mean(axis=-1, keepdims=True)
    var = x.var(axis=-1, keepdims=True)
    return (x - mu) / np.sqrt(var + eps)


def truncate_vectors(x, d):
    """Matryoshka truncation: layer-norm, keep first d dims, unit length."""
    return _unit(_layer_norm(x)[..., :d])


def hybrid_report(rows, k, rrf_k=RRF_K):
    """Which questions does fusion help or hurt? Answers the Round-3 question directly:
    does hybrid beat the better single method, and does it LOSE any answer that either
    method found on its own?"""
    ans = [r for r in rows if r["answerable"]]
    ok = lambda rk: bool(rk and rk <= k)
    n = len(ans)
    bm = {r["id"] for r in ans if ok(r["bm25_rank"])}
    md = {r["id"] for r in ans if ok(r["rank"])}
    hy = {r["id"] for r in ans if ok(r["hybrid_rank"])}
    either = bm | md
    best = max(len(bm), len(md))
    print(f"\n=== Hybrid (RRF, constant {rrf_k}) at recall@{k}, {n} answerable questions (1 question = {100 / n:.1f} points) ===")
    print(f"  BM25 {len(bm)}/{n} ({len(bm) / n:.0%})   model {len(md)}/{n} ({len(md) / n:.0%})   "
          f"either {len(either)}/{n} ({len(either) / n:.0%})   HYBRID {len(hy)}/{n} ({len(hy) / n:.0%})")
    print(f"  hybrid vs the better single method: {len(hy) - best:+d} question(s) = {100 * (len(hy) - best) / n:+.1f} points")
    lost = sorted(either - hy)
    gained = sorted(hy - md)
    print(f"  LOST by fusion (found by BM25 or model alone, not by hybrid): {', '.join(lost) or 'none'}")
    print(f"  gained over the model alone: {', '.join(gained) or 'none'}")
    print(f"  gained over BM25 alone:      {', '.join(sorted(hy - bm)) or 'none'}")
    print(f"  neither single method finds: {', '.join(sorted(r['id'] for r in ans if r['id'] not in either)) or 'none'}"
          f"   (hybrid finds: {', '.join(sorted(hy - either)) or 'none of them'})")
    print(f"  rank of first correct chunk, only where BM25 / model / hybrid differ in hit-or-miss status:")
    print(f"  {'id':5} {'BM25':>6} {'model':>6} {'hybrid':>7}")
    for r in ans:
        if len({ok(r['bm25_rank']), ok(r['rank']), ok(r['hybrid_rank'])}) > 1:
            f = lambda rk: (str(rk) if rk else "miss")
            print(f"  {r['id']:5} {f(r['bm25_rank']):>6} {f(r['rank']):>6} {f(r['hybrid_rank']):>7}")
    print("  (Offline measurement only. The app's retrieval is unchanged. RRF here is Python over the whole "
          "frozen corpus; a Postgres full-text keyword leg would rank differently, see the Batch D handoff, option A.)")


def summarize(rows, k):
    ans = [r for r in rows if r["answerable"]]
    cats = list(dict.fromkeys(r["category"] for r in ans)) + ["ALL"]
    out = {}
    for cat in cats:
        sel = ans if cat == "ALL" else [r for r in ans if r["category"] == cat]
        br, bm_ = metrics([r["bm25_rank"] for r in sel], k)
        mr, mm = metrics([r["rank"] for r in sel], k)
        hr, hm = metrics([r["hybrid_rank"] for r in sel], k)
        out[cat] = dict(n=len(sel), bm25_recall=br, bm25_mrr=bm_, model_recall=mr, model_mrr=mm,
                        hybrid_recall=hr, hybrid_mrr=hm)
    return out


def width_report(variants, k):
    """variants: list of (label, rows, summary). Prints a compact comparison."""
    labels = [v[0] for v in variants]
    print(f"\n=== Width test: recall@{k} / MRR by vector width (same vectors, cut down) ===")
    print(f"{'':12}" + "".join(f"{l:>16}" for l in labels))
    for cat in variants[0][2]:
        line = f"{cat:12}"
        for _, _, summ in variants:
            m = summ[cat]
            line += f"{m['model_recall']:>9.0%} {m['model_mrr']:>5.2f}  "
        print(line)
    base = variants[0][1]
    print(f"\nRank of the first correct chunk per question (miss = not found at all; * = hit within top {k})")
    print(f"{'id':5} {'BM25':>5}" + "".join(f"{l:>10}" for l in labels))
    for i, r in enumerate(base):
        if not r["answerable"]:
            continue
        cells = ""
        for _, rows, _ in variants:
            rk = rows[i]["rank"]
            cells += f"{(str(rk) if rk else 'miss') + ('*' if rk and rk <= k else ''):>10}"
        print(f"{r['id']:5} {str(r['bm25_rank'] or 'miss'):>5}" + cells)
    ctrl = {r["id"]: bool(r["rank"] and r["rank"] <= k) for r in base if r["answerable"]}
    print(f"\nQuestions whose hit/miss status differs from the full-width control (a flip is ~{100 / max(1, len(ctrl)):.1f} points):")
    for label, rows, _ in variants[1:]:
        flips = [r["id"] + ("+" if (r["rank"] and r["rank"] <= k) else "-")
                 for r in rows if r["answerable"] and ctrl[r["id"]] != bool(r["rank"] and r["rank"] <= k)]
        print(f"  {label:>10}: {', '.join(flips) if flips else 'none'}   (+ gained, - lost)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", required=True)
    ap.add_argument("--k", type=int, default=DEFAULT_K)
    ap.add_argument("--revision", default=None, help="pin the model to this exact commit (reproducibility)")
    ap.add_argument("--doc-prefix", default=None)
    ap.add_argument("--query-prefix", default=None)
    ap.add_argument("--trust-remote-code", action="store_true",
                    help="allow the model repo's own Python code to run (nomic needs this; only for models you trust)")
    ap.add_argument("--batch-size", type=int, default=16)
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--golden", default="golden_set.json",
                    help="golden set file(s) inside eval/, comma-separated to combine (e.g. golden_set.json,golden_set_v3.json)")
    ap.add_argument("--chunking", default="current", choices=CHUNKINGS,
                    help="Batch D Round 5: which chunking strategy to evaluate (see eval/chunkers.py); default = today's")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--dims", default=None,
                    help="comma list of vector widths to also score, e.g. 768,512,256,128 (Matryoshka models only)")
    a = ap.parse_args()

    cfg = dict(MODEL_CONFIGS.get(a.model, {}))
    if a.doc_prefix is not None: cfg["doc_prefix"] = a.doc_prefix
    if a.query_prefix is not None: cfg["query_prefix"] = a.query_prefix
    if a.trust_remote_code: cfg["trust_remote_code"] = True
    if "doc_prefix" not in cfg or "query_prefix" not in cfg:
        sys.exit(f"'{a.model}' is not in MODEL_CONFIGS. Look up its prefixes on its Hugging Face page and pass "
                 f"--doc-prefix and --query-prefix explicitly (use \"\" for none). Guessing would silently skew the score.")

    dims = None
    if a.dims:
        if not cfg.get("matryoshka"):
            sys.exit(f"--dims only works for models trained for truncation (Matryoshka). '{a.model}' is not flagged "
                     f"as one in MODEL_CONFIGS; cutting its vectors would give meaningless scores.")
        try:
            dims = sorted({int(x) for x in a.dims.split(",")}, reverse=True)
        except ValueError:
            sys.exit("--dims must be a comma list of whole numbers, e.g. 768,512,256,128")

    ver = versions()
    print("environment:", ", ".join(f"{k}={v}" for k, v in ver.items() if k in
          ("python", "sentence-transformers", "transformers", "torch", "huggingface_hub", "numpy")))

    gold = []
    for name in a.golden.split(","):
        gold += json.loads((ROOT / name.strip()).read_text(encoding="utf-8"))["questions"]
    ids = [q["id"] for q in gold]
    if len(ids) != len(set(ids)):
        raise SystemExit("duplicate question ids across the golden files: " + ", ".join(sorted({i for i in ids if ids.count(i) > 1})))
    chunks = load_chunks_variant(a.chunking)
    wc = sorted(len(c["text"].split()) for c in chunks)
    print(f"corpus: {len(chunks)} chunks (chunking: {a.chunking}, median {wc[len(wc) // 2]} words, max {wc[-1]}); {len(gold)} questions; model: {a.model}")
    print(f"prefixes: doc={cfg['doc_prefix']!r} query={cfg['query_prefix']!r}")

    t0 = time.perf_counter()
    if a.model == "fake-hash":
        model = FakeHashModel()
    else:
        from sentence_transformers import SentenceTransformer
        kw = dict(device=a.device, trust_remote_code=cfg.get("trust_remote_code", False))
        if a.revision: kw["revision"] = a.revision
        model = SentenceTransformer(a.model, **kw)
    load_s = time.perf_counter() - t0

    revision = {"model_repo": a.model if a.model == "fake-hash" else resolve_snapshot(a.model)}
    if cfg.get("code_repo"):
        revision["code_repo"] = resolve_snapshot(cfg["code_repo"])
    print(f"load time {load_s:.1f}s | cached commits actually used: {revision}")
    if not a.revision and a.model != "fake-hash":
        print("  (not pinned: re-run with --revision <model_repo commit above> to make this exactly repeatable. "
              "The code_repo commit is chosen by the library and can't be pinned this way.)")

    # Truncation check: a chunk longer than the model's window is silently cut
    # off, so any anchor in the cut part is invisible to that model.
    msl = getattr(model, "max_seq_length", None)
    trunc = None
    if msl and getattr(model, "tokenizer", None) is not None:
        lens = [len(model.tokenizer(c.get("embed_text", c["text"]), add_special_tokens=True)["input_ids"]) for c in chunks]
        trunc = sum(1 for n in lens if n > msl)
        print(f"model window: {msl} tokens | real token length of chunks: median {int(statistics.median(lens))}, "
              f"max {max(lens)} | chunks that get cut off: {trunc}/{len(chunks)}")

    # Without --dims, vectors are unit length straight from the model (previous behaviour).
    # With --dims, keep the RAW vectors once and derive every variant from them.
    keep_raw = dims is not None

    def embed_docs(texts):
        if a.model == "fake-hash":
            return np.asarray(model.encode([cfg["doc_prefix"] + t for t in texts]))
        return np.asarray(model.encode([cfg["doc_prefix"] + t for t in texts], batch_size=a.batch_size,
                                       normalize_embeddings=not keep_raw, convert_to_numpy=True,
                                       show_progress_bar=False))

    def embed_query(text):
        t = cfg["query_prefix"] + text
        if a.model == "fake-hash":
            return np.asarray(model.encode([t]))[0]
        v = model.encode([t], normalize_embeddings=not keep_raw, convert_to_numpy=True)
        return np.asarray(v)[0]

    if a.model != "fake-hash":
        embed_query("warm-up")  # first call is slower; don't count it as a query time

    variants = None
    if not keep_raw:
        rows, corpus_s, q_ms, rankings = evaluate(embed_docs, embed_query, chunks, gold, a.k)
        summary = report(rows, a.k)
        hybrid_report(rows, a.k)
    else:
        raw_docs, raw_q = {}, {}

        def cached_docs(texts):
            key = tuple(texts)
            if key not in raw_docs:
                raw_docs[key] = embed_docs(texts)
            return raw_docs[key]

        def cached_query(text):
            if text not in raw_q:
                raw_q[text] = embed_query(text)
            return raw_q[text]

        full_dim = None
        variants = []
        corpus_s = q_ms = None
        # Control first: plain full-width vectors, exactly what a run without --dims scores.
        specs = [("plain", _unit)] + [(str(d), (lambda x, d=d: truncate_vectors(x, d))) for d in dims]
        for label, fn in specs:
            if full_dim is None:
                t_embed = time.perf_counter()   # the one real embedding pass happens here, so time it here
                full_dim = cached_docs([c["text"] for c in chunks]).shape[1]
                embed_seconds = time.perf_counter() - t_embed
                too_big = [d for d in dims if d > full_dim]
                if too_big:
                    sys.exit(f"--dims {too_big} are wider than the model's {full_dim} dimensions.")
            r_, cs_, qm_, rk_ = evaluate(lambda texts, fn=fn: fn(cached_docs(texts)),
                                    lambda t, fn=fn: fn(cached_query(t)), chunks, gold, a.k)
            if corpus_s is None:           # only the first pass actually embedded anything
                corpus_s, q_ms = embed_seconds, qm_
            variants.append((label, r_, summarize(r_, a.k)))
            if label == "plain":
                rankings = rk_             # the control's rankings are the ones saved
        rows, summary = variants[0][1], variants[0][2]
        report(rows, a.k)                  # full per-question detail for the control
        hybrid_report(rows, a.k)
        width_report(variants, a.k)

    timing = dict(model_load_s=round(load_s, 2), corpus_embed_s=round(corpus_s, 2),
                  per_chunk_ms=round(corpus_s / len(chunks) * 1000, 1),
                  query_ms_median=round(statistics.median(q_ms), 1), query_ms_p95=round(pct(q_ms, 95), 1))
    print(f"\nTiming ({ver['processor'] or 'cpu'}): load {timing['model_load_s']}s | "
          f"embed {len(chunks)} chunks {timing['corpus_embed_s']}s ({timing['per_chunk_ms']} ms each) | "
          f"embed one question: median {timing['query_ms_median']} ms, p95 {timing['query_ms_p95']} ms")
    print("(Search itself is not timed here: this harness compares in memory, so it is not the app's database query.)")

    if not a.no_save:
        out = ROOT / "results"
        out.mkdir(exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", a.model) + datetime.now().strftime("_%Y%m%d_%H%M%S") + ".json"
        (out / name).write_text(json.dumps(dict(model=a.model, k=a.k, golden=a.golden, chunking=a.chunking, config=cfg, revision=revision,
                                                requested_revision=a.revision, versions=ver, truncated_chunks=trunc,
                                                timing=timing, summary=summary, rows=rows,
                                                rrf_k=RRF_K, rankings=rankings,
                                                widths=None if variants is None else
                                                {l: dict(summary=sm, rows=rw) for l, rw, sm in variants}), indent=1),
                                   encoding="utf-8")
        print(f"saved: eval/results/{name}")


if __name__ == "__main__":
    main()
