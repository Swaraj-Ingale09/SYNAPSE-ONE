"""Hybrid retrieval: full-text + vector, fused with Reciprocal Rank Fusion.

Why hybrid rather than cosine-only? Vector similarity alone misses exact terms
(a product code, an error string, a name) and full-text alone cannot match
paraphrase. Fusing both is the standard production answer, and RRF (Cormack et
al. 2009) is rank-based rather than score-based, so the two retrievers never
need their raw scores placed on a comparable scale — which they fundamentally
are not (ts_rank and cosine distance live in different worlds).

    RRF(d) = sum over retrievers of  1 / (k + rank_i(d))

with k=60 as in the original paper and in Elasticsearch/OpenSearch hybrid
search. Rank 1 contributes 1/61, not a dominating spike.

SQL shape: two CTEs produce ranked candidate ids, a UNION ALL accumulates each
id's contributions, then we join back for payloads. All user input is passed as
bound parameters through ``plainto_tsquery`` (never ``to_tsquery`` string
concatenation, which is an injection vector).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from synapse.knowledge.config import DEFAULT_SEARCH_LIMIT, RRF_K, SEARCH_POOL

log = logging.getLogger("synapse.knowledge.search")

_HYBRID_SQL = """
WITH lex AS (
    SELECT c.id AS chunk_id,
           row_number() OVER (ORDER BY ts_rank_cd(c.content_tsv, pq) DESC) AS rank
    FROM chunks c
    CROSS JOIN plainto_tsquery('english', :query) pq
    WHERE c.content_tsv @@ pq
    LIMIT :pool
),
sem AS (
    SELECT c.id AS chunk_id,
           row_number() OVER (ORDER BY c.embedding <=> CAST(:vector AS vector)) AS rank
    FROM chunks c
    WHERE c.embedding IS NOT NULL
    ORDER BY c.embedding <=> CAST(:vector AS vector)
    LIMIT :pool
),
fused AS (
    SELECT chunk_id, 1.0 / (:k + rank) AS weight FROM lex
    UNION ALL
    SELECT chunk_id, 1.0 / (:k + rank) AS weight FROM sem
)
SELECT c.id            AS chunk_id,
       c.document_id,
       c.chunk_index,
       c.content,
       c.char_start,
       c.char_end,
       d.source,
       d.title,
       d.source_type,
       sum(f.weight)   AS score
FROM fused f
JOIN chunks   c ON c.id = f.chunk_id
JOIN documents d ON d.id = c.document_id
GROUP BY c.id, c.document_id, c.chunk_index, c.content,
         c.char_start, c.char_end, d.source, d.title, d.source_type
ORDER BY score DESC
LIMIT :limit
"""

# When the vector half cannot run (no embedder) we still want lexical results
# rather than nothing at all. Honest degradation: text matching is real, we
# simply do not claim semantic recall we did not perform.
_LEXICAL_ONLY_SQL = """
WITH lex AS (
    SELECT c.id AS chunk_id,
           row_number() OVER (ORDER BY ts_rank_cd(c.content_tsv, pq) DESC) AS rank
    FROM chunks c
    CROSS JOIN plainto_tsquery('english', :query) pq
    WHERE c.content_tsv @@ pq
    LIMIT :pool
)
SELECT c.id            AS chunk_id,
       c.document_id,
       c.chunk_index,
       c.content,
       c.char_start,
       c.char_end,
       d.source,
       d.title,
       d.source_type,
       1.0 / (:k + lex.rank) AS score
FROM lex
JOIN chunks   c ON c.id = lex.chunk_id
JOIN documents d ON d.id = c.document_id
ORDER BY score DESC
LIMIT :limit
"""


@dataclass(frozen=True)
class SearchResult:
    """One retrieved chunk, with provenance back to its source."""

    chunk_id: str
    document_id: str
    chunk_index: int
    content: str
    char_start: int
    char_end: int
    source: str
    title: str | None
    source_type: str
    score: float
    retriever: str  # "hybrid" | "lexical"


class SearchEmpty(ValueError):
    """The query had no usable terms after normalisation."""


def _normalise_query(query: str) -> str:
    """Collapse whitespace and bound length; empty query is an error."""
    cleaned = " ".join(query.split())
    if not cleaned:
        raise SearchEmpty("query is empty")
    # plainto_tsquery is safe, but an absurd query is still a denial of service.
    if len(cleaned) > 2000:
        cleaned = cleaned[:2000]
    return cleaned


def search_hybrid(
    session: Session,
    *,
    query: str,
    vector: list[float] | None,
    limit: int = DEFAULT_SEARCH_LIMIT,
    pool: int = SEARCH_POOL,
    rrf_k: int = RRF_K,
) -> list[SearchResult]:
    """Run hybrid (or lexical-only) retrieval and return ranked chunks.

    ``vector`` is the embedded query. Pass ``None`` to run lexical-only — this
    is used when no embedder is configured, and results are then flagged
    ``retriever="lexical"`` so no caller can mistake it for semantic recall.
    """
    cleaned = _normalise_query(query)
    if limit <= 0:
        raise ValueError("limit must be positive")
    if vector is None:
        return _run_lexical(session, cleaned, limit, pool, rrf_k)

    params = {
        "query": cleaned,
        "vector": _to_pgvector_literal(vector),
        "limit": limit,
        "pool": pool,
        "k": rrf_k,
    }
    rows = session.execute(text(_HYBRID_SQL), params).mappings().all()
    return [_to_result(row, "hybrid") for row in rows]


def _run_lexical(
    session: Session, query: str, limit: int, pool: int, rrf_k: int
) -> list[SearchResult]:
    rows = (
        session.execute(
            text(_LEXICAL_ONLY_SQL),
            {"query": query, "limit": limit, "pool": pool, "k": rrf_k},
        )
        .mappings()
        .all()
    )
    return [_to_result(row, "lexical") for row in rows]


def _to_pgvector_literal(vector: list[float]) -> str:
    """pgvector accepts the text form ``[1,2,3]`` for casts and literals."""
    return "[" + ",".join(repr(float(v)) for v in vector) + "]"


def _to_result(row, retriever: str) -> SearchResult:  # noqa: ANN001 - RowMapping
    return SearchResult(
        chunk_id=row["chunk_id"],
        document_id=row["document_id"],
        chunk_index=row["chunk_index"],
        content=row["content"],
        char_start=row["char_start"],
        char_end=row["char_end"],
        source=row["source"],
        title=row["title"],
        source_type=row["source_type"],
        score=float(row["score"]),
        retriever=retriever,
    )
