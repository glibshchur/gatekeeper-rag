"""Structure-aware Markdown chunking.

The naive approach -- slide a fixed token window over the raw text -- destroys exactly
the structure that makes a policy document answerable. It splits tables down the middle,
severs a rule from the heading that scopes it, and cuts code fences in half.

This chunker works on blocks instead:

* Headings maintain a stack, and every chunk is prefixed with its heading path. A chunk
  that says "receipts are required above 25 USD" is useless without "Finance > Expenses"
  attached, and the prefix puts that context into the embedding rather than only into
  the metadata.
* Code fences and tables are atomic. An oversized table is split on row boundaries with
  its header repeated, because half a table with no header is not retrievable.
* Paragraphs are split on sentence boundaries, never mid-sentence.
* Overlap is carried as whole trailing blocks or sentences, not a raw token slice.

Sizing is measured with the embedding backend's own tokenizer -- see
``Embedder.count_tokens`` for why that matters.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

TokenCounter = Callable[[str], int]

_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_DIVIDER = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
# Sentence boundary: terminal punctuation, then whitespace, then something that starts a
# sentence. The lookbehind excludes a few common abbreviations that would otherwise split.
_SENTENCE = re.compile(
    r"(?<![A-Z])(?<!\be\.g)(?<!\bi\.e)(?<!\betc)(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])"
)


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CODE = "code"
    TABLE = "table"
    LIST = "list"


@dataclass
class Block:
    kind: BlockKind
    text: str
    level: int = 0

    @property
    def atomic(self) -> bool:
        """Blocks that lose their meaning when split arbitrarily."""
        return self.kind in (BlockKind.CODE, BlockKind.TABLE)


@dataclass
class TextChunk:
    ordinal: int
    content: str
    heading_path: list[str] = field(default_factory=list)
    token_count: int = 0
    oversized: bool = False


def parse_blocks(markdown: str) -> list[Block]:
    """Split Markdown into semantic blocks. Deliberately not a full CommonMark parser:
    it recognises the constructs that must not be split and treats everything else as
    prose."""
    blocks: list[Block] = []
    lines = markdown.splitlines()
    i, n = 0, len(lines)
    buffer: list[str] = []
    buffer_kind = BlockKind.PARAGRAPH

    def flush() -> None:
        nonlocal buffer, buffer_kind
        text = "\n".join(buffer).strip()
        if text:
            blocks.append(Block(buffer_kind, text))
        buffer = []
        buffer_kind = BlockKind.PARAGRAPH

    while i < n:
        line = lines[i]

        fence = _FENCE.match(line)
        if fence:
            flush()
            marker = fence.group(2)
            fenced = [line]
            i += 1
            while i < n and not lines[i].strip().startswith(marker):
                fenced.append(lines[i])
                i += 1
            if i < n:
                fenced.append(lines[i])
                i += 1
            blocks.append(Block(BlockKind.CODE, "\n".join(fenced)))
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            blocks.append(Block(BlockKind.HEADING, heading.group(2), level=len(heading.group(1))))
            i += 1
            continue

        # A table is a run of pipe rows; require the divider so that prose containing a
        # stray pipe is not misread as a one-row table.
        if _TABLE_ROW.match(line) and i + 1 < n and _TABLE_DIVIDER.match(lines[i + 1]):
            flush()
            rows = []
            while i < n and _TABLE_ROW.match(lines[i]):
                rows.append(lines[i])
                i += 1
            blocks.append(Block(BlockKind.TABLE, "\n".join(rows)))
            continue

        if not line.strip():
            flush()
            i += 1
            continue

        kind = BlockKind.LIST if _LIST_ITEM.match(line) else BlockKind.PARAGRAPH
        if buffer and kind != buffer_kind:
            flush()
        buffer_kind = kind
        buffer.append(line)
        i += 1

    flush()
    return blocks


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENTENCE.split(text)]
    return [p for p in parts if p]


def _split_table(table: str, count: TokenCounter, budget: int) -> Iterator[str]:
    """Split an oversized table on row boundaries, repeating the header in each piece."""
    rows = table.splitlines()
    header, body = rows[:2], rows[2:]
    header_text = "\n".join(header)
    header_cost = count(header_text)

    current: list[str] = []
    for row in body:
        candidate = [*current, row]
        if current and header_cost + count("\n".join(candidate)) > budget:
            yield header_text + "\n" + "\n".join(current)
            current = [row]
        else:
            current = candidate
    if current:
        yield header_text + "\n" + "\n".join(current)


def _heading_prefix(path: list[str]) -> str:
    return " > ".join(path)


def chunk_markdown(
    markdown: str,
    count: TokenCounter,
    *,
    target_tokens: int = 384,
    overlap_tokens: int = 64,
    title: str | None = None,
) -> list[TextChunk]:
    """Chunk a Markdown document.

    ``target_tokens`` defaults to 384 rather than the 1000 you often see, because the
    default local backend (bge-small) has a 512-token context. A 1000-token chunk fed to
    it is silently truncated at 512, and the tail simply never gets embedded. Chunk size
    is a property of the embedding model, not a universal constant.
    """
    blocks = parse_blocks(markdown)
    chunks: list[TextChunk] = []
    heading_stack: list[str] = [title] if title else []
    base_depth = len(heading_stack)

    pending: list[str] = []
    pending_path: list[str] = list(heading_stack)
    ordinal = 0

    def prefix_cost(path: list[str]) -> int:
        return count(_heading_prefix(path)) + 2 if path else 0

    def emit(force_oversized: bool = False) -> None:
        nonlocal pending, ordinal
        body = "\n\n".join(p for p in pending if p.strip())
        if not body.strip():
            pending = []
            return
        prefix = _heading_prefix(pending_path)
        content = f"{prefix}\n\n{body}" if prefix else body
        total = count(content)
        chunks.append(
            TextChunk(
                ordinal=ordinal,
                content=content,
                heading_path=list(pending_path),
                token_count=total,
                oversized=force_oversized or total > target_tokens,
            )
        )
        ordinal += 1
        pending = []

    def carry_overlap() -> list[str]:
        """Take whole trailing units from the chunk just emitted, up to the overlap budget."""
        if not chunks or overlap_tokens <= 0:
            return []
        tail = chunks[-1].content.split("\n\n")
        carried: list[str] = []
        used = 0
        for unit in reversed(tail):
            cost = count(unit)
            if used + cost > overlap_tokens:
                break
            carried.insert(0, unit)
            used += cost
        # Never carry the heading prefix itself; it is re-added on emit.
        if carried and carried[0] == _heading_prefix(pending_path):
            carried.pop(0)
        return carried

    def add(unit: str) -> None:
        nonlocal pending
        cost = count(unit)
        current = sum(count(p) for p in pending) + prefix_cost(pending_path)
        if pending and current + cost > target_tokens:
            emit()
            pending = carry_overlap()
        pending.append(unit)

    for block in blocks:
        if block.kind is BlockKind.HEADING:
            # A heading starts a new section; flush so sections do not bleed together.
            if pending:
                emit()
                pending = []
            depth = base_depth + block.level - 1
            heading_stack = heading_stack[:depth]
            heading_stack.append(block.text)
            pending_path = list(heading_stack)
            continue

        budget = target_tokens - prefix_cost(pending_path)
        cost = count(block.text)

        if cost <= budget:
            add(block.text)
            continue

        if block.kind is BlockKind.TABLE:
            for piece in _split_table(block.text, count, max(budget, 1)):
                if count(piece) > budget:
                    if pending:
                        emit()
                    pending = [piece]
                    emit(force_oversized=True)
                else:
                    add(piece)
            continue

        if block.atomic:
            # A code fence larger than the budget is emitted whole and flagged. Splitting
            # it would produce syntactically meaningless fragments.
            if pending:
                emit()
            pending = [block.text]
            emit(force_oversized=True)
            continue

        for sentence in split_sentences(block.text):
            if count(sentence) > budget:
                if pending:
                    emit()
                pending = [sentence]
                emit(force_oversized=True)
            else:
                add(sentence)

    emit()
    return chunks
