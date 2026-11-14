"""
Batch D, Round 5: the experimental chunkers in eval/chunkers.py (offline, pure logic, no database).
"""
import re

import pytest

pytest.importorskip("eval.chunkers", reason="eval/ is not copied into the API image; run these on the host")

from eval import chunkers as ck  # noqa: E402
from eval.chunkers import header_for, load_chunks_variant, structural_pieces  # noqa: E402

BODY = "\n".join(f'    x{i} = "alpha beta gamma delta epsilon zeta eta theta"' for i in range(8))  # ~66 words


def fn(name, deco="", comment=""):
    return f"{comment}{deco}def {name}():\n{BODY}\n    return 1\n"


def labels(pieces):
    return [p[0] for p in pieces]


def test_each_sizeable_function_is_its_own_chunk_and_keeps_its_comment_and_decorator():
    src = "import os\n\n" + fn("first") + "\n# explains second\n@deco\n" + fn("second")[0:0] + \
          "def second():\n" + BODY + "\n    return 2\n"
    pieces = structural_pieces(src, "m.py")
    texts = [p[2] for p in pieces]
    assert any(t.startswith("# explains second\n@deco\ndef second") for t in texts)
    assert sum("def first" in t for t in texts) == 1 and sum("def second" in t for t in texts) == 1
    assert not any("def first" in t and "def second" in t for t in texts)


def test_small_neighbours_are_merged_and_the_label_names_all_of_them():
    src = "def a():\n    return 1\n\ndef b():\n    return 2\n"
    pieces = structural_pieces(src, "m.py")
    assert len(pieces) == 1 and "a" in pieces[0][0] and "b" in pieces[0][0]


def test_an_oversize_function_is_split_into_parts_within_the_ceiling():
    big = "def huge():\n" + "\n".join(f"    v{i} = 'one two three four five six seven eight nine ten'" for i in range(120)) + "\n"
    pieces = structural_pieces(big, "m.py")
    assert len(pieces) > 1 and all("(part" in l for l in labels(pieces))
    assert all(len(p[2].split()) <= ck.MAX_WORDS for p in pieces)


def test_an_oversize_class_is_split_by_method():
    methods = "".join(
        f"    def m{i}(self):\n" + "\n".join(f"        a{j} = 'w1 w2 w3 w4 w5 w6 w7 w8 w9 w10'" for j in range(14)) + "\n"
        for i in range(8))
    pieces = structural_pieces("class Big:\n    '''doc'''\n" + methods, "m.py")
    assert any("Big.m3" in l for l in labels(pieces)) or any("m3" in l for l in labels(pieces))
    assert not any(len(p[2].split()) > ck.MAX_WORDS for p in pieces)


def test_python_that_does_not_parse_falls_back_to_todays_chunker():
    broken = "def oops(:\n" + "\n".join(f"line {i} with some words in it" for i in range(300))
    pieces = structural_pieces(broken, "m.py")
    assert pieces and all(p[0] == "m.py" for p in pieces)


def test_non_python_code_falls_back_to_todays_chunker():
    pieces = structural_pieces("FROM python:3.12\nRUN echo hi\n", "Dockerfile")
    assert len(pieces) == 1 and pieces[0][2].startswith("FROM")


def test_no_line_of_python_is_lost():
    src = "import os\nX = 1\n\n" + fn("first") + "\n# c\n" + fn("second") + "\nclass K:\n    def m(self):\n        return 1\n"
    blob = re.sub(r"\s+", " ", "\n".join(p[2] for p in structural_pieces(src, "m.py")))
    for line in src.splitlines():
        if line.strip():
            assert re.sub(r"\s+", " ", line.strip()) in blob


MD = "# Top\n" + ("intro words " * 35) + "\n\n## Alpha\n" + ("alpha text here. " * 40) + "\n\n## Beta\n" + ("beta text here. " * 40) + "\n"


def test_markdown_is_cut_at_headings_and_the_label_is_the_heading_path():
    pieces = structural_pieces(MD, "d.md")
    assert any(l == "Top > Alpha" for l in labels(pieces)) and any(l == "Top > Beta" for l in labels(pieces))
    assert all(p[1] == "Section" for p in pieces)


def test_a_tiny_section_is_merged_into_the_next_and_the_label_names_both():
    pieces = structural_pieces("# Top\nintro words\n\n## Alpha\n" + ("alpha text here. " * 40) + "\n", "d.md")
    assert labels(pieces) == ["Top; Top > Alpha"]


def test_a_hash_line_inside_a_code_fence_is_not_a_heading():
    md = "# Real\n```\n# just a comment\nx = 1\n```\n" + ("word " * 80) + "\n"
    assert labels(structural_pieces(md, "d.md")) == ["Real"]


def test_a_giant_block_with_no_blank_lines_is_cut_on_line_boundaries():
    lines = [f"- bullet number {i} with a handful of extra words to give it some length ok" for i in range(120)]
    pieces = structural_pieces("# S\n" + "\n".join(lines), "d.md")
    assert len(pieces) > 1
    joined = "\n".join(p[2] for p in pieces)
    assert all(l in joined for l in lines)                       # every bullet appears whole somewhere
    assert all(len(p[2].split()) <= ck.MAX_WORDS for p in pieces)


def test_header_a_is_the_path_only_and_header_c_adds_the_symbol_or_section():
    assert header_for("A_header", "app\\routers\\ingest.py", "reclassify_document", "In") == "File: app/routers/ingest.py"
    assert header_for("C_structure_header", "app\\x.py", "f", "In") == "File: app/x.py\nIn: f"
    assert header_for("C_structure_header", "d.md", "Top > A", "Section") == "File: d.md\nSection: Top > A"


def test_current_variant_is_identical_to_the_harness_chunks_used_in_every_earlier_run():
    from eval.check_golden import load_chunks
    base = [(c["file"], c["i"], c["text"]) for c in load_chunks()]
    assert [(c["file"], c["i"], c["text"]) for c in load_chunks_variant("current")] == base


def test_header_variants_change_only_what_is_embedded_never_the_matched_text():
    cur, a = load_chunks_variant("current"), load_chunks_variant("A_header")
    assert [c["text"] for c in cur] == [c["text"] for c in a]
    assert all(c["embed_text"].startswith("File: ") and c["embed_text"].endswith(c["text"]) for c in a)
    b, c = load_chunks_variant("B_structure"), load_chunks_variant("C_structure_header")
    assert [x["text"] for x in b] == [x["text"] for x in c]
    assert all(x["embed_text"] == x["text"] for x in b)


def test_structural_variants_cut_the_real_corpus_into_smaller_chunks_and_lose_no_line():
    cur, b = load_chunks_variant("current"), load_chunks_variant("B_structure")
    assert len(b) > len(cur) and max(len(c["text"].split()) for c in b) <= ck.MAX_WORDS
    from pathlib import Path
    root = Path(ck.ROOT) / "corpus"
    by_file = {}
    for c in b:
        by_file.setdefault(c["file"], []).append(c["text"])
    for p in root.rglob("*"):
        if p.is_file():
            blob = re.sub(r"\s+", " ", "\n".join(by_file.get(str(p.relative_to(root)), [])))
            for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip():
                    assert re.sub(r"\s+", " ", line.strip()) in blob, (p, line[:60])


def test_unknown_variant_is_refused():
    with pytest.raises(ValueError):
        load_chunks_variant("D_something")
