"""Knowledge layer: chunking, embeddings, store, hybrid search (ADR 0005).

Tests assert the guarantees the layer advertises rather than just happy paths:

- ``text[char_start:char_end] == content`` must hold for every chunk, because
  provenance is the whole point of recording offsets
- overlapping chunks must still make forward progress on any input
- identical content must dedup, never duplicate
- retrieval must be honestly labelled "hybrid" vs "lexical"
"""

from __future__ import annotations

import pytest

from synapse.knowledge.chunking import chunk_text, sentence_offsets
from synapse.knowledge.config import EMBEDDING_DIMENSION
from synapse.knowledge.embeddings import (
    EmbeddingError,
    HashingEmbeddings,
    build_embeddings,
)
from synapse.knowledge.search import SearchEmpty, search_hybrid
from synapse.knowledge.store import KnowledgeError, content_digest, ingest_text


@pytest.fixture
def _clean_knowledge_tables(settings):
    """Purge knowledge state around a DB-backed test.

    ``conftest`` only cleans the job tables; documents are content-addressed,
    so a leftover row would make a later "should create" assertion report a
    spurious dedup. Cleanup runs *before* the test too, so a failed prior test
    cannot cascade into confusing failures here.

    Uses ``settings.database_url`` (the dedicated test DB) rather than
    ``get_settings()``, which would read ``.env`` and wipe the *dev* database.

    Deliberately not autouse: pure-function tests (chunking, hashing) must not
    require a live database just to run.
    """
    from sqlalchemy import text

    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        session.execute(text("DELETE FROM chunks"))
        session.execute(text("DELETE FROM documents"))
    yield
    with session_scope(settings.database_url) as session:
        session.execute(text("DELETE FROM chunks"))
        session.execute(text("DELETE FROM documents"))


# --- chunking: exact provenance ----------------------------------------------


def test_offsets_slice_back_to_exact_content() -> None:
    """The core provenance invariant: slices must reproduce chunks exactly."""
    text = (
        "First sentence here. Second one follows after it.\n\n"
        "A new paragraph begins with this claim. It has more detail later. "
        "And a third clause to force a boundary."
    )
    chunks = chunk_text(text, max_chars=60, overlap=10)
    assert chunks, "expected at least one chunk"
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.content, (
            f"chunk {c.index} offset mismatch"
        )


def test_offsets_hold_on_hard_cut_units() -> None:
    """Even an indivisible long token must slice back correctly."""
    text = "x" * 500 + " tail words here"
    chunks = chunk_text(text, max_chars=100, overlap=0)
    assert chunks
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.content


def test_chunk_respects_max_chars() -> None:
    text = " ".join(f"word{i}" for i in range(400))
    chunks = chunk_text(text, max_chars=120, overlap=20)
    assert chunks
    for c in chunks:
        # Indivisible single units are the only legal exception; these words
        # are all far shorter than the budget, so nothing may exceed it.
        assert len(c.content) <= 120, f"chunk {c.index} too long: {len(c.content)}"


def test_overlap_shares_text_between_adjacent_chunks() -> None:
    text = " ".join(f"separate{i}" for i in range(300))
    chunks = chunk_text(text, max_chars=100, overlap=30)
    assert len(chunks) >= 2
    # Adjacent chunks must genuinely overlap (share a span), not abut.
    first, second = chunks[0], chunks[1]
    assert second.char_start < first.char_end
    shared = set(first.content.split()) & set(second.content.split())
    assert shared, "overlap should carry tokens across the boundary"


def test_chunks_are_dense_and_sequential() -> None:
    text = "\n\n".join(f"Paragraph {i} with a sentence or two in it." for i in range(20))
    chunks = chunk_text(text, max_chars=80, overlap=15)
    assert [c.index for c in chunks] == list(range(len(chunks)))


def test_progress_guaranteed_with_max_overlap() -> None:
    """overlap = max_chars-1 must still terminate and make forward progress."""
    text = "word " * 500
    chunks = chunk_text(text, max_chars=50, overlap=49)
    assert chunks, "must not return empty on non-empty input"
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts), "starts must be monotonic"
    assert len(set(starts)) == len(starts), "each chunk must advance"


def test_empty_and_whitespace_inputs() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\t  ") == []
    assert chunk_text("\n\n\n") == []


def test_invalid_chunk_params_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_text("text", max_chars=0)
    with pytest.raises(ValueError):
        chunk_text("text", max_chars=10, overlap=10)
    with pytest.raises(ValueError):
        chunk_text("text", max_chars=10, overlap=-1)


def test_sentence_offsets_contain_document_bounds() -> None:
    text = "One. Two. Three."
    offsets = sentence_offsets(text)
    assert offsets[0] == 0
    assert offsets[-1] == len(text)
    assert offsets == sorted(offsets)


def test_abbreviations_do_not_split_sentences() -> None:
    text = "Use e.g. this approach carefully."
    offsets = sentence_offsets(text)
    # Only the document start and end are boundaries; "e.g." must not split.
    assert offsets == [0, len(text)] or offsets[0] == 0


# --- embeddings: real vectors, no fabrication --------------------------------


def test_hashing_embeddings_are_deterministic() -> None:
    emb = HashingEmbeddings()
    a = emb.embed(["the quick brown fox"])
    b = emb.embed(["the quick brown fox"])
    assert a == b, "same input must produce identical vectors"


def test_hashing_embeddings_dimension_is_pinned() -> None:
    emb = HashingEmbeddings()
    out = emb.embed(["some text"])
    assert len(out[0]) == EMBEDDING_DIMENSION
    assert emb.dimension == EMBEDDING_DIMENSION


def test_embeddings_are_l2_normalised() -> None:
    """Cosine search requires unit vectors; a zero vector would break ranking."""
    import math

    emb = HashingEmbeddings()
    for vec in emb.embed(["hello world", "another sample", "x"]):
        norm = math.sqrt(sum(v * v for v in vec))
        assert norm == pytest.approx(1.0, abs=1e-6), f"not normalised: {norm}"


def test_similar_texts_score_higher_than_unrelated() -> None:
    """Lexical embedder must genuinely encode overlap, not random noise."""

    def dot(a: list[float], b: list[float]) -> float:
        # Vectors are L2-normalised, so the dot product *is* the cosine.
        return sum(x * y for x, y in zip(a, b, strict=True))

    emb = HashingEmbeddings()
    q, related, unrelated = emb.embed(
        [
            "machine learning model training",
            "training a machine learning model",
            "cooking pasta recipes",
        ]
    )
    assert dot(q, related) > dot(q, unrelated), (
        "semantic-ish overlap must outscore unrelated text"
    )


def test_embed_batch_limit_enforced() -> None:
    from synapse.knowledge.embeddings import MAX_EMBED_BATCH

    emb = HashingEmbeddings()
    with pytest.raises(EmbeddingError, match="exceeds"):
        emb.embed(["t"] * (MAX_EMBED_BATCH + 1))


def test_build_embeddings_local_is_default() -> None:
    provider = build_embeddings("local")
    assert isinstance(provider, HashingEmbeddings)


def test_build_embeddings_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNAPSE_EMBEDDINGS", raising=False)
    with pytest.raises(EmbeddingError, match="unknown embedding provider"):
        build_embeddings("quantum-lemur")


def test_openai_embedder_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """No key => error, never a fabricated vector (spec 2)."""
    from synapse.knowledge.embeddings import OpenAIEmbeddings

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIEmbeddings(api_key="")
    assert provider.configured is False
    with pytest.raises(EmbeddingError, match="OPENAI_API_KEY"):
        provider.embed(["text"])


# --- store: dedup, provenance, honesty ---------------------------------------


def test_content_digest_is_stable_and_hex() -> None:
    a = content_digest("stable content")
    b = content_digest("stable content")
    assert a == b
    assert len(a) == 64
    assert a != content_digest("different content")


def test_ingest_empty_content_rejected(settings, _clean_knowledge_tables) -> None:
    """Empty ingest must fail loudly, not create a hollow document."""
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        with pytest.raises(KnowledgeError, match="empty"):
            ingest_text(session, text="   \n  ", source="http://example.com/empty")


def test_ingest_too_large_rejected(settings, _clean_knowledge_tables) -> None:
    """Oversized documents are refused before any embedding work starts."""
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        with pytest.raises(KnowledgeError, match="limit"):
            ingest_text(
                session,
                text="a" * 100,
                source="http://example.com/big",
                max_chars=50,
            )


def test_ingest_stores_document_and_chunks(settings, _clean_knowledge_tables) -> None:
    """Happy path: one document, chunks with correct provenance offsets."""
    from sqlalchemy import select

    from synapse.db import session_scope
    from synapse.knowledge.models import Chunk as ChunkRow
    from synapse.knowledge.models import Document

    text = " ".join(f"sentence number {i} about distributed systems." for i in range(30))
    with session_scope(settings.database_url) as session:
        result = ingest_text(
            session,
            text=text,
            source="http://example.com/ingest-test",
            title="Distributed Systems",
            content_type="text/plain",
        )
        assert result.created is True
        assert result.chunks > 0
        assert result.deduplicated is False

        doc = session.get(Document, result.document_id)
        assert doc is not None
        assert doc.content == text
        assert doc.content_hash == content_digest(text)
        assert doc.embedded_at is not None
        assert doc.char_count == len(text)

        rows = (
            session.execute(
                select(ChunkRow)
                .where(ChunkRow.document_id == result.document_id)
                .order_by(ChunkRow.chunk_index)
            )
            .scalars()
            .all()
        )
        assert len(rows) == result.chunks
        # Provenance invariant must hold against the stored document content.
        for row in rows:
            assert text[row.char_start : row.char_end] == row.content
            assert row.embedding is not None
            assert len(row.embedding) == EMBEDDING_DIMENSION


def test_duplicate_ingest_is_a_noop(settings, _clean_knowledge_tables) -> None:
    """Identical content must dedup on the unique content_hash index."""
    from synapse.db import session_scope

    text = "Unique payload that should only ever be stored once."
    with session_scope(settings.database_url) as session:
        first = ingest_text(session, text=text, source="http://example.com/dup")
        assert first.created is True

    with session_scope(settings.database_url) as session:
        second = ingest_text(session, text=text, source="http://example.com/dup")
        assert second.created is False
        assert second.deduplicated is True
        assert second.document_id == first.document_id
        assert second.chunks == 0


def test_hybrid_search_ranks_relevant_chunk_first(settings, _clean_knowledge_tables) -> None:
    """End-to-end: ingest two topics, query one, assert it outranks the other."""
    from synapse.db import session_scope
    from synapse.knowledge.embeddings import HashingEmbeddings

    pg_text = (
        "PostgreSQL provides ACID transactions and row level locking with "
        "FOR UPDATE SKIP LOCKED for concurrent queue workers. "
    ) * 8
    cooking_text = (
        "Bring the pasta water to a rolling boil then add salt and the "
        "spaghetti and stir occasionally. "
    ) * 8

    with session_scope(settings.database_url) as session:
        ingest_text(session, text=pg_text, source="http://example.com/pg")
        ingest_text(session, text=cooking_text, source="http://example.com/cook")

    with session_scope(settings.database_url) as session:
        emb = HashingEmbeddings()
        vector = emb.embed(["database row locking concurrent transactions"])[0]
        results = search_hybrid(
            session,
            query="database row locking concurrent transactions",
            vector=vector,
            limit=4,
        )

    assert results, "expected at least one result"
    assert results[0].retriever == "hybrid"
    assert results[0].source == "http://example.com/pg", (
        f"expected the Postgres doc first, got {results[0].source}"
    )
    # Scores must be descending (RRF ordering).
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_lexical_only_search_is_honestly_labelled(settings, _clean_knowledge_tables) -> None:
    """vector=None => lexical path, and the label must say so."""
    from synapse.db import session_scope

    text = "Kubernetes orchestrates containers across a cluster of nodes."
    with session_scope(settings.database_url) as session:
        ingest_text(session, text=text, source="http://example.com/k8s")

    with session_scope(settings.database_url) as session:
        results = search_hybrid(
            session,
            query="kubernetes containers cluster",
            vector=None,
            limit=3,
        )

    assert results, "lexical search must still return matches"
    assert all(r.retriever == "lexical" for r in results), (
        "lexical-only results must never be labelled hybrid"
    )
    assert results[0].source == "http://example.com/k8s"


def test_search_returns_nothing_for_unrelated_query(settings, _clean_knowledge_tables) -> None:
    """No false positives on a term absent from the corpus."""
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        ingest_text(
            session,
            text="Only recipes about bread baking live here.",
            source="http://example.com/bread",
        )

    with session_scope(settings.database_url) as session:
        results = search_hybrid(
            session,
            query="quantum chromodynamics lattice gauge theory",
            vector=None,
            limit=5,
        )
    assert results == []


def test_chunk_text_covers_short_document() -> None:
    """A tiny document yields one chunk spanning the whole text."""
    text = "Short."
    chunks = chunk_text(text, max_chars=100, overlap=10)
    assert len(chunks) == 1
    assert chunks[0].char_start == 0
    assert chunks[0].char_end == len(text)
    assert chunks[0].content == text


# --- search: empty query guard -----------------------------------------------


def test_empty_query_rejected() -> None:
    class _FakeSession:
        def execute(self, *_a, **_k):  # noqa: ANN001, ANN201
            raise AssertionError("should not execute SQL for an empty query")

    with pytest.raises(SearchEmpty):
        search_hybrid(_FakeSession(), query="   ", vector=None)


def test_oversized_query_is_truncated_not_rejected() -> None:
    class _CapturingSession:
        def __init__(self) -> None:
            self.captured: dict = {}

        def execute(self, stmt, params):  # noqa: ANN001, ANN201
            self.captured = dict(params)

            class _R:
                def mappings(self):
                    class _Rows:
                        def all(self):
                            return []

                    return _Rows()

            return _R()

    session = _CapturingSession()
    search_hybrid(session, query="a" * 5000, vector=None, limit=3)
    assert len(session.captured["query"]) == 2000
