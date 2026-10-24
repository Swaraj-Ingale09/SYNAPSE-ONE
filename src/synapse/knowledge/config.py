"""Knowledge-layer tuning constants (ADR 0005).

``EMBEDDING_DIMENSION`` is fixed for the whole system so that every provider's
vectors are interchangeable in the same ``vector(1024)`` column, and so a
provider switch never requires a table rebuild. OpenAI's 3-series embedding
models accept an explicit ``dimensions`` parameter, so 1024 is available without
inventing a projection step; the local hashing embedder emits 1024 directly.
"""

from __future__ import annotations

# --- embedding ---------------------------------------------------------------

EMBEDDING_DIMENSION = 1024

# OpenAI 3-series accepts `dimensions`; pinned so it matches the column above.
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDING_DIMENSIONS = 1024

# --- chunking ----------------------------------------------------------------

# ~500 tokens for English prose at ~4 chars/token: large enough that a chunk
# carries a whole claim, small enough that retrieval is precise.
CHUNK_MAX_CHARS = 2000
# Overlap keeps a sentence that straddles a boundary retrievable from both sides.
CHUNK_OVERLAP_CHARS = 200
# Refuse pathological inputs early rather than emitting millions of rows.
DOCUMENT_MAX_CHARS = 2_000_000

# --- retrieval ---------------------------------------------------------------

# Reciprocal Rank Fusion constant. 60 is the value from the original Cormack et
# al. paper and is what Elasticsearch/OpenSearch hybrid search defaults to; it
# damps the contribution of rank #1 so one retriever cannot dominate the fuse.
RRF_K = 60
# Candidate pool each retriever contributes to before fusion.
SEARCH_POOL = 50
DEFAULT_SEARCH_LIMIT = 8

# --- embedding request limits ------------------------------------------------

# Cap per embed call so one giant document cannot blow the provider payload.
EMBED_BATCH_CHARS = 32_000
