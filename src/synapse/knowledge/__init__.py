"""Knowledge layer: ingestion, chunking, embedding, hybrid retrieval (ADR 0005).

Pipeline (spec 8):

    fetch -> extract -> chunk -> embed -> store -> hybrid search

Design commitments:

- **Provenance** — every chunk records its exact ``char_start``/``char_end`` in
  the original document, so an answer is always traceable to source text.
- **Dedup** — documents are content-addressed (SHA-256); re-ingesting the same
  bytes is a no-op rather than a duplicate row.
- **Honest retrieval** — results are labelled ``"hybrid"`` or ``"lexical"``.
  When no embedder is configured we say so instead of implying semantic recall.
- **Real embeddings, no simulation** — see :mod:`synapse.knowledge.embeddings`.
  The default local provider genuinely computes vectors (feature hashing); it is
  lexical, and documented as lexical.
- **External content is data, never policy** (spec 15.3): nothing in this
  package executes, renders, or interprets ingested text as instructions.
"""

from synapse.knowledge.chunking import Chunk, chunk_text
from synapse.knowledge.config import EMBEDDING_DIMENSION
from synapse.knowledge.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    HashingEmbeddings,
    OpenAIEmbeddings,
    build_embeddings,
)
from synapse.knowledge.search import SearchEmpty, SearchResult, search_hybrid
from synapse.knowledge.store import (
    IngestResult,
    KnowledgeError,
    content_digest,
    ingest_text,
)

__all__ = [
    "Chunk",
    "DEFAULT_SEARCH_LIMIT",
    "EmbeddingError",
    "EmbeddingProvider",
    "EMBEDDING_DIMENSION",
    "HashingEmbeddings",
    "IngestResult",
    "KnowledgeError",
    "OpenAIEmbeddings",
    "SearchEmpty",
    "SearchResult",
    "build_embeddings",
    "chunk_text",
    "content_digest",
    "ingest_text",
    "search_hybrid",
]

# Re-exported for convenience: callers sizing results need the default limit
# without importing the config module separately.
from synapse.knowledge.config import DEFAULT_SEARCH_LIMIT  # noqa: E402
