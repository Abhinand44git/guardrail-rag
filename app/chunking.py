"""
Chunking for ingestion (Phase 4). Decision log, Session 5 addendum #2:
"Start with fixed-size chunking, applied per
source_type; record the real chunk_strategy value honestly per row (e.g.
'fixed_500_words', not something vague)." Deliberately a first pass, not a
finished design — chunk_strategy is a free-text per-row column specifically
so a smarter (semantic/structural) strategy can be swapped in later without
touching rows already written under this one. Same category of "will look
primitive next to a 'designed for extensibility' framing" as the project's
existing fixed-roles-only note (decision log reminders).

The unit is WORDS: whitespace-delimited runs of non-space characters, not the
embedding model's subword tokens (ADR-0015). This module used to call that unit
"tokens", which was misleading: measured with the real tokenizer on this
project's own corpus, a 500-word chunk comes out at a median of about 943 model
tokens (max about 1,588), so the numbers are not interchangeable and the ratio
varies from chunk to chunk. ADR-0015 changed only the NAMES and the recorded
label, not the chunk boundaries -- the chunks themselves are identical. Chunk size matters for
retrieval quality, so whether to size chunks by real tokens is decided with the
chunk-size experiments on the eval harness, not guessed here. Rows written
before this rename carry the older label 'fixed_500_tokens'; it means the same
thing as today's 'fixed_500_words'.

Same fixed-size *strategy* is applied uniformly across all three
source_types, per the decision's own wording — no source-type-aware
"smart" chunking (AST-based for code, section-based for markdown) yet. The
one per-source_type difference here is structural, not strategic: code is
chunked along line boundaries so a chunk never splits a line in half,
because a partial line is nonsense on either side of the cut in a way a
partial sentence in prose usually isn't. Flagged as an implementation
choice made while applying the locked decision, not a separate decision.
"""
from __future__ import annotations

import re

# Written down explicitly (not buried as a bare literal) since this number
# is also what gets stringified into chunk_strategy below — the two must
# always match, or the recorded strategy would lie about what actually ran.
DEFAULT_TARGET_WORDS = 500
DEFAULT_OVERLAP_WORDS = 50

_WORD_RE = re.compile(r"\S+")


def chunk_strategy_label(target_words: int = DEFAULT_TARGET_WORDS) -> str:
    """The honest, per-row chunk_strategy value -- e.g. 'fixed_500_words'."""
    return f"fixed_{target_words}_words"


def chunk_text(
    text: str,
    source_type: str,
    target_words: int = DEFAULT_TARGET_WORDS,
    overlap_words: int = DEFAULT_OVERLAP_WORDS,
) -> list[str]:
    """
    Splits `text` into fixed-size (word-count) chunks with a small
    overlap between consecutive chunks, so a fact sitting right at a chunk
    boundary isn't invisible to retrieval on both sides of the cut. Empty
    or whitespace-only input returns no chunks (nothing to embed).
    """
    if not text or not text.strip():
        return []

    if source_type == "code":
        return _chunk_by_lines(text, target_words, overlap_words)
    return _chunk_by_words(text, target_words, overlap_words)


def _chunk_by_words(text: str, target_words: int, overlap_words: int) -> list[str]:
    words = _WORD_RE.findall(text)
    if not words:
        return []

    step = max(1, target_words - overlap_words)
    chunks = []
    for start in range(0, len(words), step):
        chunk_words = words[start : start + target_words]
        if not chunk_words:
            break
        chunks.append(" ".join(chunk_words))
        if start + target_words >= len(words):
            break
    return chunks


def _chunk_by_lines(text: str, target_words: int, overlap_words: int) -> list[str]:
    """
    Same fixed-size-with-overlap approach as _chunk_by_words, but grouped by
    whole lines rather than raw word count, so a chunk boundary never falls
    inside a single line of code. A line's size is its whitespace-delimited
    word count (minimum 1 for a blank line).

    Known limitation, not handled here: a single line longer than
    target_words (e.g. a minified file) still becomes its own oversized
    chunk rather than being split further — the "never split a line"
    guarantee wins over the size target in that case. Acceptable for a
    first pass (real source files are the expected input, not minified
    output); revisit if minified/generated code is ever a real ingestion
    target.
    """
    lines = text.splitlines()
    if not lines:
        return []

    line_word_counts = [len(_WORD_RE.findall(line)) or 1 for line in lines]

    chunks = []
    start = 0
    n = len(lines)
    while start < n:
        end = start
        total = 0
        while end < n and (total == 0 or total + line_word_counts[end] <= target_words):
            total += line_word_counts[end]
            end += 1
        chunks.append("\n".join(lines[start:end]))
        if end >= n:
            break

        # Step back by roughly overlap_words worth of lines for the next
        # chunk's start, without ever going backwards past `start` (which
        # would loop forever on a single very long line).
        back = end
        overlap_so_far = 0
        while back > start + 1 and overlap_so_far < overlap_words:
            back -= 1
            overlap_so_far += line_word_counts[back]
        start = back

    return chunks
