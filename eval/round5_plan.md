# Batch D, Round 5: does smarter chunking beat fixed 500-word chunks?

**Written before any result existed. Commit this file before running the experiment.**

## Question
Today `app/chunking.py` cuts every 500 words (code: on line boundaries), whatever the content means. Golden
question C8's answer sits in a 411-word chunk that holds three functions, starts inside an unrelated comment and
never names its file; P15's answer sits in a 500-word chunk that starts mid-sentence with no heading. Do chunks
that follow the content, and/or carry a header, find more answers?

## Variants (nothing else is tried)
| name | boundaries | embedded text |
|---|---|---|
| `current` | today's (the baseline) | chunk |
| `A_header` | today's | `File: <path>` + chunk |
| `B_structure` | Python by function/class, Markdown by heading section | chunk |
| `C_structure_header` | as B | `File: <path>` + `In:`/`Section:` name + chunk |

Fixed sizes for B and C, not tuned: at most 500 words per chunk; units under 60 words are merged with a
neighbour; an oversize unit is split on line boundaries (Markdown: on paragraphs first). Dockerfile/YAML and any
file that does not parse keep today's chunker. Only what is *embedded* gets a header; the stored chunk text and the
golden-answer matching use the plain text. Model, revision and prefixes stay exactly as in the earlier runs.

## Rule (applied by `python -m eval.decide_chunking`)
A variant is adoptable only if ALL FIVE hold on the 53 answerable questions (golden v2 + v3), comparing the
embedding model's ranking with today's chunking:
1. at least +5.0 points of recall at BOTH k=5 and k=10;
2. MRR not lower (strict);
3. no category finds more than one fewer question, at k=5 or k=10;
4. v2 alone and v3 alone each find at least as many as today's, at k=5 and k=10;
5. the average text in the top-5 chunks is at least 50% of today's (smaller chunks must not starve the caller).

If several are adoptable, the simplest wins (A, then B, then C). If none is, the app keeps today's chunking.

## Limits, stated up front
- Three variants on questions already used for the hybrid decision (part of v3 was written with the corpus visible).
  The two-set consistency condition and the +5-at-both-depths bar limit, but do not remove, the chance of a lucky pass.
- Chunk count changes (112 vs about 207), so "rank of the first correct chunk" is not perfectly like for like.
- Nothing in the app changes in this round. A winner would be ported in a separate, tested change (Round 6).
