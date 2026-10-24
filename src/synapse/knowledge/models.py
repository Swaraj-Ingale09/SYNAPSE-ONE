"""SQLAlchemy models for the knowledge layer: documents and chunks (ADR 0005).

Mirrors migration ``0003_knowledge``. The ``tsvector`` and ``embedding``
columns are managed by Postgres (generated column and provider writes
respectively), so they are read-mostly here.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from synapse.knowledge.config import EMBEDDING_DIMENSION

# Postgres timestamptz, used for every timestamp column in this schema.
DateTimeTZ = DateTime(timezone=True)


def _utcnow() -> datetime:
    from datetime import UTC
    from datetime import datetime as _dt

    return _dt.now(UTC)


class KnowledgeBase(DeclarativeBase):
    pass


class Document(KnowledgeBase):
    """One ingested source of knowledge, content-addressed for dedup."""

    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_content_hash", "content_hash", unique=True),
        Index("ix_documents_source", "source"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(16), nullable=False, default="url")
    title: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    char_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16))
    ingested_by: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    created_at: Mapped[datetime] = mapped_column(
        DateTimeTZ, nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTimeTZ, nullable=False, default=_utcnow, server_default=func.now()
    )
    embedded_at: Mapped[datetime | None] = mapped_column(DateTimeTZ)

    chunks: Mapped[list[Chunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="Chunk.chunk_index",
        passive_deletes=True,
    )


class Chunk(KnowledgeBase):
    """A retrievable span of a document with its embedding."""

    __tablename__ = "chunks"
    __table_args__ = (
        Index("ix_chunks_document_id_chunk_index", "document_id", "chunk_index"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSION), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTimeTZ, nullable=False, default=_utcnow, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="chunks")
