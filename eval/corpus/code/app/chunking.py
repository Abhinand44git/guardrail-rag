"""
Chunking for ingestion (Phase 4). Decision log, Session 5 addendum #2:
"Start with fixed-size chunking (token-count based), applied per
source_type; record the real chunk_strategy value honestly per row (e.g.
'fixed_500_tokens', not something vague)." Deliberately a first pass, not a
finished design — chunk_strategy is a free-text per-row column specifically
so a smarter (semantic/structural) strategy can be swapped in later without
touching rows already written under this one. Same category of "will look
primitive next to a 'designed for extensibility' framing" as the project's
existing fixed-roles-only note (decision log reminders).

"Token" here means an approximate whitespace-delimited word count, not the
embedding model's real subword tokenization — using the model's own
tokenizer would mean loading the model just to chunk text, coupling this
module to app.embeddings for no real accuracy gain at this stage. Flagged
here rather than silently treated as exact; revisit if chunk sizes turn out
to matter precisely (e.g. against a hard context-window limit) rather than
just approximately.

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
DEFAULT_TARGET_TOKENS = 500
DEFAULT_OVERLAP_TOKENS = 50

_WORD_RE = re.compile(r"\S+")


def chunk_strategy_label(target_tokens: int = DEFAULT_TARGET_TOKENS) -> str:
    """The honest, per-row chunk_strategy value — e.g. 'fixed_500_tokens',
    the exact example given in the decision log."""
    return f"fixed_{target_tokens}_tokens"


def chunk_text(
    text: str,
    source_type: str,
    target_tokens: int = DEFAULT_TARGET_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> list[str]:
    """
    Splits `text` into fixed-size (approx. token-count) chunks with a small
    overlap between consecutive chunks, so a fact sitting right at a chunk
    boundary isn't invisible to retrieval on both sides of the cut. Empty
    or whitespace-only input returns no chunks (nothing to embed).
    """
    if not text or not text.strip():
        return []

    if source_type == "code":
        return _chunk_by_lines(text, target_tokens, overlap_tokens)
    return _chunk_by_words(text, target_tokens, overlap_tokens)


def _chunk_by_words(text: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    words = _WORD_RE.findall(text)
    if not words:
        return []

    step = max(1, target_tokens - overlap_tokens)
    chunks = []
    for start in range(0, len(words), step):
        chunk_words = words[start : start + target_tokens]
        if not chunk_words:
            break
        chunks.append(" ".join(chunk_words))
        if start + target_tokens >= len(words):
            break
    return chunks


def _chunk_by_lines(text: str, target_tokens: int, overlap_tokens: int) -> list[str]:
    """
    Same fixed-size-with-overlap approach as _chunk_by_words, but grouped by
    whole lines rather than raw word count, so a chunk boundary never falls
    inside a single line of code. "Tokens" per line is still approximated
    as that line's whitespace-delimited word count.

    Known limitation, not handled here: a single line longer than
    target_tokens (e.g. a minified file) still becomes its own oversized
    chunk rather than being split further — the "never split a line"
    guarantee wins over the size target in that case. Acceptable for a
    first pass (real source files are the expected input, not minified
    output); revisit if minified/generated code is ever a real ingestion
    target.
    """
    lines = text.splitlines()
    if not lines:
        return []

    line_token_counts = [len(_WORD_RE.findall(line)) or 1 for line in lines]

    chunks = []
    start = 0
    n = len(lines)
    while start < n:
        end = start
        total = 0
        while end < n and (total == 0 or total + line_token_counts[end] <= target_tokens):
            total += line_token_counts[end]
            end += 1
        chunks.append("\n".join(lines[start:end]))
        if end >= n:
            break

        # Step back by roughly overlap_tokens worth of lines for the next
        # chunk's start, without ever going backwards past `start` (which
        # would loop forever on a single very long line).
        back = end
        overlap_so_far = 0
        while back > start + 1 and overlap_so_far < overlap_tokens:
            back -= 1
            overlap_so_far += line_token_counts[back]
        start = back

    return chunks
