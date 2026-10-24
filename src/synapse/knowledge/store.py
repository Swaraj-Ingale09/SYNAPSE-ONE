"""Knowledge store: ingest documents, chunk, embed, persist (ADR 0005).

Responsibilities kept here rather than in a task so the API, worker, and CLI all
share one ingestion path:

- **dedup by content hash** — re-ingesting identical bytes is a no-op, not a
  duplicate row (the unique index enforces it, this just handles it gracefully)
- **chunk then embed then insert** — an unembedded document is never silently
  searchable as if it were fully indexed; ``embedded_at`` records completion
- **bounded** — oversized documents are refused, batches respect provider limits
- **provenance** — every chunk keeps its exact source span and its document id

Nothing here treats fetched content as instructions (spec 15.3): content is
stored as inert text and is only ever returned to a caller as data.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from synapse.knowledge.chunking import Chunk, chunk_text
from synapse.knowledge.config import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
    DOCUMENT_MAX_CHARS,
    EMBED_BATCH_CHARS,
)
from synapse.knowledge.embeddings import EmbeddingProvider, build_embeddings

log = logging.getLogger("synapse.knowledge.store")


class KnowledgeError(RuntimeError):
    """Ingestion refused (too large, no content, embedding failure)."""


def content_digest(text: str) -> str:
    """SHA-256 of the UTF-8 bytes, hex. The dedup key for documents."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class IngestResult:
    document_id: str
    created: bool  # False => already present, no work done
    chunks: int
    characters: int
    deduplicated: bool = False


def find_by_hash(session: Session, digest: str) -> str | None:
    """Return the existing document id for this content hash, if any."""
    from synapse.knowledge.models import Document

    return session.execute(
        select(Document.id).where(Document.content_hash == digest)
    ).scalar_one_or_none()


def _batch_texts_for_embedding(chunks: list[Chunk], limit_chars: int) -> list[list[Chunk]]:
    """Group chunks into embed batches bounded by total characters.

    Keeps one request from exceeding a provider payload limit even when a
    document produced many chunks.
    """
    batches: list[list[Chunk]] = []
    current: list[Chunk] = []
    running = 0
    for chunk in chunks:
        size = len(chunk.content)
        if current and running + size > limit_chars:
            batches.append(current)
            current, running = [], 0
        current.append(chunk)
        running += size
    if current:
        batches.append(current)
    return batches


def ingest_text(
    session: Session,
    *,
    text: str,
    source: str,
    source_type: str = "url",
    title: str | None = None,
    content_type: str | None = None,
    language: str | None = None,
    metadata: dict | None = None,
    ingested_by: str = "local",
    embedder: EmbeddingProvider | None = None,
    max_chars: int = DOCUMENT_MAX_CHARS,
    chunk_max_chars: int = CHUNK_MAX_CHARS,
    chunk_overlap: int = CHUNK_OVERLAP_CHARS,
) -> IngestResult:
    """Chunk, embed, and store one document. Idempotent on identical content.

    Raises :class:`KnowledgeError` rather than partially committing a document
    whose embeddings could not be produced: an unsearchable row would mislead
    the user into thinking retrieval covered it.
    """
    from synapse.knowledge.models import Chunk as ChunkRow
    from synapse.knowledge.models import Document

    if not text or not text.strip():
        raise KnowledgeError("cannot ingest empty content")
    if len(text) > max_chars:
        raise KnowledgeError(
            f"document is {len(text)} chars, limit is {max_chars}"
        )

    digest = content_digest(text)
    existing = find_by_hash(session, digest)
    if existing is not None:
        return IngestResult(
            document_id=existing,
            created=False,
            chunks=0,
            characters=len(text),
            deduplicated=True,
        )

    spans = chunk_text(text, max_chars=chunk_max_chars, overlap=chunk_overlap)
    if not spans:
        raise KnowledgeError("chunking produced no chunks")

    provider = embedder or build_embeddings()
    embeddings = _embed_chunks(provider, spans)

    document_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    session.add(
        Document(
            id=document_id,
            source=source,
            source_type=source_type,
            title=title,
            content=text,
            content_hash=digest,
            content_type=content_type,
            char_count=len(text),
            metadata=metadata or {},
            language=language,
            ingested_by=ingested_by,
            created_at=now,
            updated_at=now,
            embedded_at=now,
        )
    )

    for span, vector in zip(spans, embeddings, strict=True):
        session.add(
            ChunkRow(
                id=str(uuid.uuid4()),
                document_id=document_id,
                chunk_index=span.index,
                content=span.content,
                char_start=span.char_start,
                char_end=span.char_end,
                token_estimate=max(1, len(span.content) // 4),
                embedding=vector,
                created_at=now,
            )
        )

    log.info(
        "ingested_document id=%s chunks=%d chars=%d dedup=false embedder=%s",
        document_id,
        len(spans),
        len(text),
        provider.name,
    )
    return IngestResult(
        document_id=document_id,
        created=True,
        chunks=len(spans),
        characters=len(text),
    )


def _embed_chunks(provider: EmbeddingProvider, spans: list[Chunk]) -> list[list[float]]:
    """Embed every chunk in bounded batches. Raises on any failure."""
    batches = _batch_texts_for_embedding(spans, EMBED_BATCH_CHARS)
    vectors: list[list[float]] = []
    for batch in batches:
        produced = provider.embed([c.content for c in batch])
        if len(produced) != len(batch):
            raise KnowledgeError(
                f"embedder returned {len(produced)} vectors for {len(batch)} chunks"
            )
        vectors.extend(produced)
    if len(vectors) != len(spans):
        raise KnowledgeError(
            f"embedding count mismatch: {len(vectors)} != {len(spans)}"
        )
    return vectors
