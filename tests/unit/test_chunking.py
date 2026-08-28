"""Chunker tests.

Token counting uses a whitespace counter rather than a real tokenizer: these tests are
about structure, and a deterministic counter makes the boundaries assertable.
"""

from __future__ import annotations

from gatekeeper.ingest.chunking import (
    BlockKind,
    chunk_markdown,
    parse_blocks,
    split_sentences,
)


def words(text: str) -> int:
    return len(text.split())


TABLE_DOC = """# Expenses

Some prose before the table.

| Item | Limit | Approver |
| --- | --- | --- |
| Meals | 75 USD | Manager |
| Flights | 1200 USD | Director |
| Hotels | 300 USD | Manager |

Some prose after.
"""

CODE_DOC = """# Runbook

Run the following:

```bash
kubectl get pods -n production
kubectl logs deploy/api --tail 100
```

Then check the dashboard.
"""


# --- block parsing ---------------------------------------------------------


def test_tables_are_parsed_as_single_blocks() -> None:
    kinds = [b.kind for b in parse_blocks(TABLE_DOC)]
    assert kinds.count(BlockKind.TABLE) == 1
    table = next(b for b in parse_blocks(TABLE_DOC) if b.kind is BlockKind.TABLE)
    assert "Meals" in table.text and "Hotels" in table.text


def test_prose_with_a_stray_pipe_is_not_a_table() -> None:
    # Without the `| --- |` divider this is just prose, and treating it as a table would
    # make the chunker refuse to split a paragraph.
    blocks = parse_blocks("Use the `a | b` syntax when piping.\n")
    assert all(b.kind is not BlockKind.TABLE for b in blocks)


def test_code_fences_are_atomic_and_keep_their_markers() -> None:
    code = next(b for b in parse_blocks(CODE_DOC) if b.kind is BlockKind.CODE)
    assert code.text.startswith("```bash")
    assert code.text.rstrip().endswith("```")
    assert "kubectl logs" in code.text


def test_headings_record_their_level() -> None:
    blocks = parse_blocks("# One\n\ntext\n\n### Three\n\nmore\n")
    headings = [(b.text, b.level) for b in blocks if b.kind is BlockKind.HEADING]
    assert headings == [("One", 1), ("Three", 3)]


def test_lists_and_paragraphs_do_not_merge() -> None:
    blocks = parse_blocks("Intro paragraph.\n\n- first\n- second\n")
    assert [b.kind for b in blocks] == [BlockKind.PARAGRAPH, BlockKind.LIST]


# --- sentence splitting ----------------------------------------------------


def test_sentence_split_respects_common_abbreviations() -> None:
    assert split_sentences("Submit receipts, e.g. meals. Then wait.") == [
        "Submit receipts, e.g. meals.",
        "Then wait.",
    ]


def test_sentence_split_does_not_break_decimals() -> None:
    assert len(split_sentences("The limit is 25.50 USD per day.")) == 1


# --- chunking --------------------------------------------------------------


def test_every_chunk_carries_its_heading_path() -> None:
    doc = "# Finance\n\n## Expenses\n\nReceipts are required above 25 USD.\n"
    chunks = chunk_markdown(doc, words, target_tokens=50, title="Handbook")
    assert chunks[0].heading_path == ["Handbook", "Finance", "Expenses"]
    # The path is in the text too, so it reaches the embedding rather than only metadata.
    assert chunks[0].content.startswith("Handbook > Finance > Expenses")


def test_heading_stack_pops_on_a_shallower_heading() -> None:
    doc = "# A\n\n## B\n\ntext b\n\n# C\n\ntext c\n"
    chunks = chunk_markdown(doc, words, target_tokens=50)
    paths = [c.heading_path for c in chunks]
    assert ["A", "B"] in paths
    assert ["C"] in paths


def test_chunks_stay_within_the_target() -> None:
    doc = "# Policy\n\n" + "\n\n".join(f"Sentence number {i} about policy." for i in range(80))
    chunks = chunk_markdown(doc, words, target_tokens=40, overlap_tokens=8)
    assert len(chunks) > 1
    assert all(c.token_count <= 40 for c in chunks if not c.oversized)


def test_overlap_repeats_trailing_content() -> None:
    doc = "# P\n\n" + "\n\n".join(f"Unit {i} carries distinct words here." for i in range(30))
    with_overlap = chunk_markdown(doc, words, target_tokens=40, overlap_tokens=12)
    without = chunk_markdown(doc, words, target_tokens=40, overlap_tokens=0)
    assert len(with_overlap) >= len(without)
    # Something from the tail of chunk 1 must reappear at the head of chunk 2.
    tail = with_overlap[0].content.split("\n\n")[-1]
    assert tail in with_overlap[1].content


def test_a_code_fence_within_budget_stays_intact() -> None:
    small = "```py\nx = 1\ny = 2\n```"
    chunks = chunk_markdown(f"# R\n\n{small}\n", words, target_tokens=200)
    assert any(c.content.count("```") == 2 for c in chunks)


def test_an_oversized_code_fence_is_split_on_line_boundaries_and_flagged() -> None:
    """Splitting a code fence produces syntactically meaningless fragments, which is why
    an earlier version emitted it whole. That was wrong: whole means the embedder
    truncates it at its context limit, so the tail is not merely fragmented but absent.
    A fragment that can be retrieved beats a fragment that cannot."""
    big = "```py\n" + "\n".join(f"line_{i} = {i}" for i in range(200)) + "\n```"
    chunks = chunk_markdown(f"# R\n\n{big}\n", words, target_tokens=30)
    assert len(chunks) > 1
    assert all(c.token_count <= 30 for c in chunks)
    assert all(c.oversized for c in chunks)
    # Splitting on line boundaries keeps individual statements readable.
    assert "line_0 = 0" in chunks[0].content
    assert "line_199 = 199" in chunks[-1].content


def test_an_oversized_table_splits_on_rows_and_repeats_the_header() -> None:
    rows = "\n".join(f"| item{i} | {i} USD | Manager |" for i in range(60))
    doc = f"# Limits\n\n| Item | Limit | Approver |\n| --- | --- | --- |\n{rows}\n"
    chunks = chunk_markdown(doc, words, target_tokens=60)
    table_chunks = [c for c in chunks if "| Item | Limit | Approver |" in c.content]
    assert len(table_chunks) > 1, "the table should have been split"
    for chunk in table_chunks:
        # Every piece must be independently interpretable.
        assert "| Item | Limit | Approver |" in chunk.content


def test_a_short_table_is_never_split() -> None:
    chunks = chunk_markdown(TABLE_DOC, words, target_tokens=200)
    holding = [c for c in chunks if "Meals" in c.content]
    assert holding and all("Hotels" in c.content for c in holding)


def test_ordinals_are_contiguous_from_zero() -> None:
    doc = "# A\n\n" + "\n\n".join(f"Paragraph {i} here." for i in range(40))
    chunks = chunk_markdown(doc, words, target_tokens=20)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_empty_and_whitespace_documents_yield_nothing() -> None:
    assert chunk_markdown("", words) == []
    assert chunk_markdown("\n\n   \n", words) == []


def test_a_document_with_only_headings_yields_nothing() -> None:
    # There is no content to retrieve, and a chunk containing only a breadcrumb would be
    # noise that outranks real answers on short queries.
    assert chunk_markdown("# A\n\n## B\n\n### C\n", words) == []


# --- the last-resort split -------------------------------------------------


def test_a_giant_unsplittable_blob_is_split_rather_than_truncated() -> None:
    """Regression. `handbook/security/corporate/systems/_index.md` is built from raw HTML
    <table> markup: no Markdown table syntax, no sentence boundaries. It reached the
    chunker as one 45,343-token unit, was emitted whole, and the embedder then truncated
    it at 512 tokens -- putting ~99% of the page in the database as unretrievable text,
    with no error raised anywhere."""
    blob = " ".join(f"<td>cell{i}</td>" for i in range(4000))
    chunks = chunk_markdown(f"# Systems\n\n{blob}\n", words, target_tokens=100)
    assert len(chunks) > 10
    assert all(c.token_count <= 100 for c in chunks)
    assert all(c.oversized for c in chunks[1:]), "degraded splits must be flagged as such"


def test_no_chunk_can_exceed_the_target_regardless_of_input() -> None:
    # The invariant the bug above violated. `oversized` now means "split without
    # respecting semantic boundaries", never "larger than the model can embed".
    pathological = [
        "x" * 50_000,  # one enormous word, no break opportunities at all
        "\n".join("y" * 400 for _ in range(100)),  # long lines, no spaces
        "word " * 20_000,  # one very long line of ordinary words
        "```\n" + ("z = 1\n" * 5_000) + "```",  # an enormous code fence
    ]
    for body in pathological:
        chunks = chunk_markdown(f"# T\n\n{body}\n", words, target_tokens=80)
        assert chunks, "pathological input must still produce chunks"
        assert all(c.token_count <= 80 for c in chunks), f"overflow on {body[:20]!r}"


def test_hard_split_prefers_whitespace_boundaries() -> None:
    chunks = chunk_markdown("# T\n\n" + "alpha beta gamma " * 500, words, target_tokens=60)
    # No chunk should begin or end mid-word when spaces were available to break on.
    for chunk in chunks[1:]:
        body = chunk.content.split("\n\n", 1)[-1]
        assert body.split()[0] in {"alpha", "beta", "gamma"}


def test_content_is_preserved_across_a_hard_split() -> None:
    marker_count = 300
    blob = "".join(f"<td>MARK{i}</td>" for i in range(marker_count))
    chunks = chunk_markdown(f"# T\n\n{blob}\n", words, target_tokens=50)
    joined = "".join(c.content for c in chunks)
    missing = [i for i in range(marker_count) if f"MARK{i}" not in joined]
    assert not missing, f"{len(missing)} markers lost by the split"
