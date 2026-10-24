# ADR 0005: Knowledge layer — chunking, embeddings, hybrid retrieval

## Status

Accepted — 2026-10-24

## Context

SYNAPSE ONE must answer questions from material it has ingested (documents,
fetched pages), not only from a model's parametric memory. That requires a
retrieval layer with three properties this project refuses to compromise on:

- **Provenance.** Every retrieved fragment must map back to an exact span of
  its source, so an answer is always checkable against the text it came from.
- **Honesty about capability.** With no API key the system must not pretend to
  have semantic understanding it lacks.
- **Real engine behaviour.** Tests run against live Postgres + pgvector, so
  generated columns, HNSW indexes, and rank fusion are genuinely exercised
  rather than mocked.

## Decision

### Schema (`0003_knowledge`)

- **`documents`** are content-addressed: `content_hash` (SHA-256) carries a
  **unique index**, so re-ingesting identical bytes is a no-op, never a
  duplicate row. `embedded_at` records when indexing completed — a document is
  never silently treated as searchable before its vectors exist.
- **`chunks`** store `char_start`/`char_end` spans, the raw text, and a
  `vector(1024)` embedding.
- **Fixed `EMBEDDING_DIMENSION = 1024`** for the whole system, so vectors from
  any provider are interchangeable in the same column and a provider switch
  never requires a table rebuild. OpenAI's 3-series accepts an explicit
  `dimensions` parameter, so 1024 is available without inventing a projection
  step; the local provider emits 1024 directly.
- **`content_tsv` is a stored generated column**
  (`to_tsvector('english', content)`) with a **GIN index**. Because
  `to_tsvector(regconfig, text)` is IMMUTABLE, Postgres keeps it in sync
  automatically — no trigger, no application code that can drift.
- **HNSW** (`vector_cosine_ops`, m=16, ef_construction=64) rather than IVFFlat:
  better recall/latency on small corpora and incremental inserts without a
  rebuild step.

### Chunking (`knowledge/chunking.py`)

- **Boundaries are computed first, then the original text is sliced.** A chunk
  may only end at a sentence boundary (hard cut only for an indivisible run
  longer than the budget). Because every chunk is a direct `text[start:end]`
  slice, the invariant `text[char_start:char_end] == content` holds **by
  construction** — provenance cannot drift.
- **Overlap** (200 chars) makes a claim straddling a boundary retrievable from
  either side; the next start is snapped forward to a word boundary so no chunk
  begins mid-word, and forward progress is enforced (`next_start > start`) so
  no input can cause non-termination.
- Sentence splitting respects trailing abbreviations (`e.g.`, `Dr.`, …).

### Embeddings (`knowledge/embeddings.py`)

Two real providers, no simulation:

1. **`HashingEmbeddings`** (default, keyless) — signed feature hashing over
   word unigrams **and bigrams** (the "hashing trick", Weinberger et al. 2009),
   FNV-1a hashed for determinism across processes, L2-normalised. This is a
   genuine, long-standing NLP technique that genuinely encodes lexical overlap:
   similar documents genuinely rank closer. It is documented as **lexical, not
   semantic** — "car"/"automobile" will not match — and the code says so
   loudly rather than implying transformer-grade recall it does not have.
   Signed hashing plus an all-zero guard prevent the failure mode where every
   document is equidistant from every query.
2. **`OpenAIEmbeddings`** — real semantic vectors, dimension-pinned to 1024.

Neither fabricates a result. The OpenAI provider **raises** on missing key, HTTP
error, or malformed response; it never falls back to emitting a hash vector
under the caller's belief that they asked for semantic embeddings.

### Retrieval (`knowledge/search.py`)

- **Hybrid = Postgres full-text + pgvector cosine, fused with Reciprocal Rank
  Fusion** (Cormack et al. 2009): `RRF(d) = Σ 1/(k + rank_i(d))`, k=60 as in
  the paper and in Elasticsearch/OpenSearch hybrid search. RRF is rank-based,
  so `ts_rank` and cosine distance never have to be placed on a comparable
  scale — which they fundamentally are not.
- Two CTEs produce ranked candidate ids, `UNION ALL` accumulates each id's
  contributions, then join back for payloads.
- All user input passes as **bound parameters through `plainto_tsquery`** —
  never `to_tsquery` string concatenation, which is an injection vector.
- **Honest degradation:** with no embedder, `vector=None` runs the lexical-only
  query and every result is labelled `retriever="lexical"`, so no caller can
  mistake it for semantic recall. Hybrid results are labelled `"hybrid"`.

## Consequences

- Ingestion is idempotent and bounded (2M chars per document, 32k chars per
  embed batch, 128 texts per request), so one hostile or huge document cannot
  blow provider payload limits or bloat the database.
- Tests (`tests/test_knowledge.py`, 29 cases) assert the advertised guarantees,
  not just happy paths: exact offset reconstruction, forward progress under
  maximal overlap, dedup on the unique index, descending RRF scores, and
  retriever labelling — all against live Postgres.
- The default embedder's lexical-only recall is a **known, documented
  limitation**. Semantic quality requires configuring `SYNAPSE_EMBEDDINGS=openai`
  and a key; the system states this rather than disguising the difference.
- Content is stored and returned as **inert text**; nothing executes, renders,
  or interprets ingested content as instructions (spec 15.3).
- Chunking is pure and deterministic — same input, same chunks — so tests need
  no clock, network, or randomness control.
