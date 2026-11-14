"""
Batch D, Round 5: experimental chunking strategies. EVAL ONLY -- nothing here is used by the app.

Today's chunker (app/chunking.py) cuts every 500 words (code: on line boundaries), whatever the content
means. A real example: the chunk that answers golden question C8 ("which function lets only an Admin
change how visible an uploaded document is?") holds THREE functions, starts in the middle of a comment
about PDF errors, and never names its file. Its vector is a blend, so nobody finds it.

Four variants, written down BEFORE any result existed (see eval/round5_plan.md):

  current             today's chunks, nothing added (the baseline).
  A_header            the SAME boundaries, but what gets embedded has a header on top: the file path.
  B_structure         boundaries follow the content: Python by function/class (comments and decorators stay
                      with their function), Markdown by heading section. No header.
  C_structure_header  B's boundaries plus a header: the file path and the function or section name.

Only what is EMBEDDED changes with a header. The stored/returned chunk text, and what the golden answers are
matched against, stays the plain chunk text.

Size rules for B and C, fixed in advance (not tuned): at most MAX_WORDS (500, same ceiling as today) per
chunk, units under MIN_WORDS (60) are merged with a neighbour, oversize units are split again.
Anything that is not Python or Markdown (Dockerfile, YAML) falls back to today's line-based chunker.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
from app.chunking import (  # noqa: E402
    DEFAULT_OVERLAP_WORDS,
    _chunk_by_lines,
    chunk_text as current_chunk_text,
)

MAX_WORDS = 500
MIN_WORDS = 60
VARIANTS = ("current", "A_header", "B_structure", "C_structure_header")
SIMPLICITY_ORDER = VARIANTS[1:]  # when several variants pass, the earliest here wins (simplest)

_words = lambda s: len(s.split())


# ---------------------------------------------------------------------------------------------- Python
def _cover(lines, start, end, nodes, gap_label):
    """Partition lines[start:end] into (label, a, b, node) spans: one per node (its decorators and the comment
    block directly above it included) and one for each stretch of other code between them (node None)."""
    spans, pos = [], start
    for node in nodes:
        s = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])]) - 1
        while s > pos and lines[s - 1].lstrip().startswith("#"):
            s -= 1
        if s > pos:
            spans.append((gap_label, pos, s, None))
        spans.append((node.name, s, node.end_lineno, node))
        pos = node.end_lineno
    if pos < end:
        spans.append((gap_label, pos, end, None))
    return spans


def _py_units(text):
    """[(label, text)] following the structure of a Python file, or None if it does not parse."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    lines = text.split("\n")
    defs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    units = []
    for label, a, b, node in _cover(lines, 0, len(lines), defs, "(module level)"):
        chunk = "\n".join(lines[a:b])
        if isinstance(node, ast.ClassDef) and _words(chunk) > MAX_WORDS:
            methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for mlabel, ma, mb, mnode in _cover(lines, a, b, methods, f"{label} (class body)"):
                units.append((f"{label}.{mlabel}" if mnode is not None else mlabel, "\n".join(lines[ma:mb])))
        else:
            units.append((label, chunk))
    return [(l, t) for l, t in units if t.strip()]


# ---------------------------------------------------------------------------------------------- Markdown
def _md_units(text):
    """[(heading path, text)]: one unit per heading section. '#' lines inside code fences are not headings."""
    sections, stack, cur, path, fenced = [], [], [], "(preamble)", False

    def flush():
        body = "\n".join(cur)
        if body.strip():
            sections.append((path, body))

    for line in text.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        m = None if fenced else re.match(r"^(#{1,6})\s+(\S.*?)\s*$", line)
        if m:
            flush()
            level = len(m.group(1))
            stack = [(l, t) for l, t in stack if l < level] + [(level, m.group(2))]
            path = " > ".join(t for _, t in stack)
            cur = [line]
        else:
            cur.append(line)
    flush()
    return sections


def _split_by_paragraphs(text, max_words):
    pieces, buf, size = [], [], 0
    for para in re.split(r"\n\s*\n", text):
        w = _words(para)
        if w > max_words:  # one giant block (e.g. a long bullet list with no blank lines): cut it on LINE
            if buf:        # boundaries so a bullet is never split in half
                pieces.append("\n\n".join(buf)); buf, size = [], 0
            pieces.extend(_chunk_by_lines(para, max_words, DEFAULT_OVERLAP_WORDS))
        elif size + w > max_words and buf:
            pieces.append("\n\n".join(buf)); buf, size = [para], w
        else:
            buf.append(para); size += w
    if buf:
        pieces.append("\n\n".join(buf))
    return [p for p in pieces if p.strip()]


# ---------------------------------------------------------------------------------------------- size rules
def _pack(units, split_oversize):
    """Apply the fixed size rules to [(label, text)]: split anything over MAX_WORDS, then merge neighbours
    while one of them is under MIN_WORDS and the result stays within MAX_WORDS."""
    sized = []
    for label, text in units:
        if _words(text) > MAX_WORDS:
            parts = split_oversize(text)
            for i, p in enumerate(parts, 1):
                sized.append((f"{label} (part {i}/{len(parts)})" if len(parts) > 1 else label, p))
        else:
            sized.append((label, text))
    out = []
    for label, text in sized:
        if out and (_words(out[-1][1]) < MIN_WORDS or _words(text) < MIN_WORDS) \
                and _words(out[-1][1]) + _words(text) <= MAX_WORDS:
            prev_label, prev_text = out[-1]
            labels = prev_label.split("; ")
            out[-1] = ("; ".join(labels + ([label] if label not in labels else [])), prev_text + "\n\n" + text)
        else:
            out.append((label, text))
    return out


def structural_pieces(text, path):
    """[(label, kind, chunk_text)] with boundaries that follow the content (variants B and C)."""
    suffix = Path(path).suffix
    if suffix == ".py":
        units = _py_units(text)
        if units is not None:
            return [(l, "In", t) for l, t in _pack(units, lambda t: _chunk_by_lines(t, MAX_WORDS, DEFAULT_OVERLAP_WORDS))]
    elif suffix == ".md":
        return [(l, "Section", t) for l, t in _pack(_md_units(text), lambda t: _split_by_paragraphs(t, MAX_WORDS))]
    st = "markdown" if suffix == ".md" else "code"  # unparseable or non-Python code: today's chunker
    return [(Path(path).name, "In", c) for c in current_chunk_text(text, st)]


def header_for(variant, rel_path, label, kind):
    """What is put on top of a chunk before embedding. A: file path only. C: path plus function/section."""
    h = f"File: {rel_path.replace(chr(92), '/')}"
    if variant == "C_structure_header" and label:
        h += f"\n{kind}: {label}"
    return h


def load_chunks_variant(variant, corpus_root=None):
    """The frozen corpus chunked by `variant`: [{file, i, text, embed_text}]. Same file order and the same
    keys as eval.check_golden.load_chunks, so rankings and golden anchors keep working."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown chunking variant {variant!r}; choose from {VARIANTS}")
    root = Path(corpus_root) if corpus_root else ROOT / "corpus"
    chunks = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(root))
        text = p.read_text(encoding="utf-8", errors="replace")
        st = "markdown" if p.suffix == ".md" else "code"
        if variant in ("current", "A_header"):
            pieces = [(None, None, c) for c in current_chunk_text(text, st)]
        else:
            pieces = structural_pieces(text, rel)
        for i, (label, kind, chunk) in enumerate(pieces):
            embed = chunk if variant in ("current", "B_structure") else header_for(variant, rel, label, kind) + "\n\n" + chunk
            chunks.append({"file": rel, "i": i, "text": chunk, "embed_text": embed})
    return chunks
