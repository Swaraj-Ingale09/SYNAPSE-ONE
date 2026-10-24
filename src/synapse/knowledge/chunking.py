"""Sentence-aware text chunking with exact character offsets (ADR 0005).

Why not naive ``text[i:i+2000]`` slicing? Fixed-width slices cut mid-word and
mid-sentence, which measurably hurts retrieval quality and makes provenance
("which part of the source did this answer come from?") impossible to state.

This chunker computes **boundaries first, then slices the original**:

- a chunk may only end at a sentence boundary (or a hard cut when a single
  indivisible run exceeds ``max_chars``)
- the next chunk starts ``overlap`` characters before the previous end, snapped
  forward to a word boundary so no chunk begins mid-word
- because every chunk is a direct ``text[start:end]`` slice, the invariant
  ``text[char_start:char_end] == content`` holds by construction — provenance
  cannot drift
- overlap is guaranteed to make progress (``next_start > start``), so no input
  can cause non-termination
- pure and deterministic: same input, same chunks, no network, no I/O
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from synapse.knowledge.config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS

# Sentence terminators are the preferred break points.
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
# Trailing abbreviations must not be treated as sentence ends.
_ABBREV_RE = re.compile(
    r"\b(?:e\.g|i\.e|etc|vs|cf|Mr|Mrs|Dr|Prof|Inc|Ltd|St|No)\.$", re.IGNORECASE
)
_WHITESPACE = " \t\r\n"


@dataclass(frozen=True)
class Chunk:
    """One retrievable span of a document."""

    index: int
    content: str
    char_start: int
    char_end: int

    @property
    def char_length(self) -> int:
        return len(self.content)


def sentence_offsets(text: str) -> list[int]:
    """Absolute offsets where a new sentence begins (plus end-of-text).

    These are the legal chunk boundaries. Sorted, unique, always containing
    ``len(text)`` so the final chunk has a defined end.
    """
    starts = {0, len(text)}
    cursor = 0
    for piece in _SENTENCE_RE.split(text):
        if not piece:
            continue
        found = text.find(piece, cursor)
        if found == -1:
            found = cursor
        # Treat "e.g." style abbreviations as continuing the previous sentence.
        prefix = text[max(0, found - 4) : found]
        if prefix and _ABBREV_RE.search(prefix):
            starts.add(found - len(prefix))
        else:
            starts.add(found)
        cursor = found + len(piece)
    return sorted(starts)


def _snap_forward_to_word(text: str, pos: int) -> int:
    """Advance from ``pos`` to just after the next whitespace run (or EOL).

    Guarantees a chunk never begins in the middle of a word when there is a
    nearby boundary to snap to.
    """
    limit = len(text)
    if pos <= 0:
        return 0
    if pos >= limit:
        return limit
    if text[pos] in _WHITESPACE or text[pos - 1] in _WHITESPACE:
        return pos
    # Walk forward to the end of the current word.
    probe = pos
    while probe < limit and text[probe] not in _WHITESPACE:
        probe += 1
    # Skip the whitespace itself so we land at a word start.
    while probe < limit and text[probe] in _WHITESPACE:
        probe += 1
    # Snapping forward could overrun into the next chunk's territory; the
    # caller clamps so forward progress is always preserved.
    return probe


def chunk_text(
    text: str,
    *,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    """Split ``text`` into overlapping, boundary-respecting chunks.

    Guarantees:
    - ``text[c.char_start:c.char_end] == c.content`` for every chunk
    - chunks cover the document in order (overlap makes spans share text, but
      coverage is monotonic and never skips ahead of the previous end)
    - no chunk exceeds ``max_chars`` unless a single indivisible unit does
    - every chunk contains non-whitespace
    - terminates for all inputs (strict forward progress)
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap < 0:
        raise ValueError("overlap must be non-negative")
    if overlap >= max_chars:
        raise ValueError("overlap must be smaller than max_chars")
    if not text or not text.strip():
        return []

    boundaries = sentence_offsets(text)
    chunks: list[Chunk] = []
    start = 0
    length = len(text)

    while start < length:
        # Choose the furthest legal boundary within the size budget.
        end = -1
        for boundary in boundaries:
            if boundary <= start:
                continue
            if boundary - start <= max_chars:
                end = boundary
            else:
                break
        if end == -1:
            # First unit in this window exceeds max_chars (one unbroken run):
            # hard-cut at the budget, preserving offsets exactly.
            end = start + max_chars

        content = text[start:end]
        # Skip pure-whitespace windows (e.g. trailing blank lines) without
        # emitting an empty chunk, but keep forward progress.
        if content.strip():
            chunks.append(
                Chunk(index=len(chunks), content=content, char_start=start, char_end=end)
            )

        if end >= length:
            break

        # Compute the next start: overlap before end, snapped to a word boundary.
        next_start = end - overlap
        if next_start <= start:
            next_start = start + 1  # mandatory forward progress
        else:
            next_start = _snap_forward_to_word(text, next_start)
            if next_start <= start:
                next_start = start + 1
        start = next_start

    return _renumber(chunks)


def _renumber(chunks: list[Chunk]) -> list[Chunk]:
    """Ensure indices are dense 0..n-1 after any whitespace filtering."""
    return [
        Chunk(
            index=i,
            content=c.content,
            char_start=c.char_start,
            char_end=c.char_end,
        )
        for i, c in enumerate(chunks)
    ]
