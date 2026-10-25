"""Worker × knowledge integration: ingest persists, ask grounds (ADR 0007).

Honesty invariants asserted here:

- an ``ingest_url`` job's text really lands in the knowledge store (document
  rows exist, chunk counts are factual) — not merely in a transient result;
- re-ingesting identical content is a dedup no-op with a factual flag, never a
  second document;
- an ask's ``sources`` are non-empty only when retrieval actually matched
  stored chunks, and every source traces back to a real row;
- explicit context usage bypasses retrieval entirely;
- a retrieval failure degrades to ungrounded (``sources=[]``) with an event
  note — it must never invent context or fail the ask itself.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

from synapse.config import Settings
from synapse.contracts import JobStatus
from synapse.db import session_scope
from synapse.db.models import Job
from synapse.gateway.types import ModelRef
from synapse.knowledge.models import Chunk, Document
from synapse.knowledge.store import content_digest, ingest_text
from synapse.net.fetch import FetchResult
from synapse.net.html_text import extract_text
from synapse.worker.tasks import (
    INGEST_MAX_CHARS,
    TaskContext,
    TaskFailure,
    ask_task,
    ingest_url_task,
)

_FENCE_START = "BEGIN UNTRUSTED CONTENT"


def _ctx(settings: Settings, task_type: str = "ask") -> TaskContext:
    """A TaskContext backed by a live job row (events carry a jobs FK)."""
    job_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    with session_scope(settings.database_url) as session:
        session.add(
            Job(
                id=job_id,
                task_type=task_type,
                status=JobStatus.RUNNING.value,
                payload={},
                correlation_id=correlation_id,
                attempts=1,
                max_attempts=3,
            )
        )
    return TaskContext(settings=settings, job_id=job_id, correlation_id=correlation_id)


def _all_job_events(settings: Settings) -> list[tuple[str, Any]]:
    """Return (event_type, metadata) pairs, detached from the session."""
    from synapse.db.models import JobEvent

    with session_scope(settings.database_url) as session:
        rows = session.execute(
            select(JobEvent.event_type, JobEvent.metadata_).order_by(JobEvent.id)
        ).all()
        return [(event_type, metadata) for event_type, metadata in rows]


def _ingest_doc(settings: Settings, *, text: str, title: str = "Fixture Doc") -> str:
    with session_scope(settings.database_url) as session:
        result = ingest_text(
            session, text=text, source="test://fixture", source_type="test", title=title
        )
    return result.document_id


class _FakeFetcher:
    """Context-manager fetcher replaying canned FetchResults."""

    def __init__(self, results: list[FetchResult]) -> None:
        self._results = list(results)
        self.fetched_urls: list[str] = []

    def __enter__(self) -> _FakeFetcher:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def fetch(self, url: str) -> FetchResult:
        self.fetched_urls.append(url)
        if not self._results:
            raise AssertionError("fake fetcher ran out of scripted results")
        return self._results.pop(0)


@pytest.fixture
def gated_gateway(monkeypatch: pytest.MonkeyPatch):
    """Replace build_gateway with a scripted gateway that captures messages."""
    from synapse.gateway.router import GatewayUsage, ModelReply

    captured: dict[str, Any] = {}

    class _FakeGateway:
        def __enter__(self) -> _FakeGateway:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def ask(self, messages, *, model=None, system=None, max_tokens=1024, job_id=None):
            captured["messages"] = list(messages)
            captured["model_arg"] = model
            return ModelReply(
                text="grounded answer",
                model=ModelRef(provider="local", model_id="mimo-v2.6-flash-free"),
                usage=GatewayUsage(
                    input_tokens=10,
                    output_tokens=5,
                    # Price unknown — the result must keep it unknown, not fake 0.
                    cost_usd=None,
                    price_known=False,
                    price=None,
                    attempts=1,
                    latency_ms=12,
                ),
                stop_reason="end_turn",
            )

    monkeypatch.setattr("synapse.gateway.build_gateway", lambda settings: _FakeGateway())
    return captured


# --- ingest_url → knowledge store -------------------------------------------------


def test_ingest_url_stores_text_in_knowledge(
    settings: Settings, _clean_knowledge_tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch's text must persist as a real document, not just a result."""
    body = (
        "<html><head><title>Quantum Platypus Nocturne</title></head><body>"
        "<p>The quantum platypus nocturne hums at 42 hertz inside the synthflow lab."
        "</p><p>Retrieval over this text must be possible after ingestion.</p>"
        "</body></html>"
    )
    fake = _FakeFetcher(
        [
            FetchResult(
                url="https://example.com/platypus",
                status_code=200,
                content_type="text/html; charset=utf-8",
                text=body,
                bytes_read=len(body),
            )
        ]
    )
    monkeypatch.setattr("synapse.net.build_fetcher", lambda _s: fake)

    ctx = _ctx(settings, task_type="ingest_url")
    result = ingest_url_task({"url": "https://example.com/platypus"}, ctx)

    # Same extraction path the task used — the comparison is like for like.
    _title, expected_text = extract_text(body, max_chars=INGEST_MAX_CHARS)
    knowledge = result["knowledge"]
    assert knowledge is not None
    assert knowledge["created"] is True
    assert knowledge["deduplicated"] is False
    assert knowledge["chunks"] >= 1

    with session_scope(settings.database_url) as session:
        doc = session.get(Document, knowledge["document_id"])
        assert doc is not None, "the ingest must have persisted a real document row"
        assert doc.content_hash == content_digest(expected_text)
        assert doc.char_count == len(expected_text)
        assert doc.source == "https://example.com/platypus"
        stored_chunks = session.execute(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id)
        ).scalar_one()
        assert stored_chunks == knowledge["chunks"]

    # The result text is a bounded preview; the complete text lives in the store.
    assert result["chars"] == len(expected_text)
    assert result["text"] == expected_text[:2000] or len(result["text"]) <= 2000

    events = _all_job_events(settings)
    stored = [m for t, m in events if t == "knowledge_stored"]
    assert stored, "persistence must be observable in the event stream"
    assert stored[-1]["document_id"] == knowledge["document_id"]


def test_ingest_url_dedups_identical_content(
    settings: Settings, _clean_knowledge_tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Identical content re-ingested is a factual no-op, never a second row."""
    body = "<html><body><p>quantum platypus nocturne at 42 hertz</p></body></html>"

    def one_result(_s):  # noqa: ANN001
        return _FakeFetcher(
            [
                FetchResult(
                    url="https://example.com/platypus",
                    status_code=200,
                    content_type="text/html",
                    text=body,
                    bytes_read=len(body),
                )
            ]
        )

    monkeypatch.setattr("synapse.net.build_fetcher", one_result)
    first = ingest_url_task({"url": "https://example.com/platypus"}, _ctx(settings, "ingest_url"))
    second = ingest_url_task({"url": "https://example.com/platypus"}, _ctx(settings, "ingest_url"))

    assert first["knowledge"]["created"] is True
    assert second["knowledge"]["deduplicated"] is True
    assert second["knowledge"]["document_id"] == first["knowledge"]["document_id"]

    with session_scope(settings.database_url) as session:
        count = session.execute(select(func.count()).select_from(Document)).scalar_one()
    assert count == 1, "content-addressed persistence must dedup identical fetches"

    events = _all_job_events(settings)
    assert any(t == "knowledge_deduplicated" for t, _m in events)


def test_ingest_url_store_false_keeps_fetch_only(
    settings: Settings, _clean_knowledge_tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    """store=false is the honest fetch-only mode: nothing lands in knowledge."""
    plain = "plain quantum platypus text"
    fake = _FakeFetcher(
        [
            FetchResult(
                url="https://example.com/plain",
                status_code=200,
                content_type="text/plain",
                text=plain,
                bytes_read=len(plain),
            )
        ]
    )
    monkeypatch.setattr("synapse.net.build_fetcher", lambda _s: fake)

    result = ingest_url_task({"url": "https://example.com/plain", "store": False}, _ctx(settings))

    assert result["knowledge"] is None
    assert result["text"] == plain
    with session_scope(settings.database_url) as session:
        count = session.execute(select(func.count()).select_from(Document)).scalar_one()
    assert count == 0


def test_knowledge_storage_error_fails_the_ingest(
    settings: Settings, _clean_knowledge_tables, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fetch that cannot be stored must fail the job, never lie about success."""
    fake = _FakeFetcher(
        [
            FetchResult(
                url="https://example.com/plain",
                status_code=200,
                content_type="text/plain",
                text="will not be stored",
                bytes_read=18,
            )
        ]
    )
    monkeypatch.setattr("synapse.net.build_fetcher", lambda _s: fake)

    import synapse.knowledge.store as store_mod

    def refuse(session, **_kw: Any) -> Any:
        raise store_mod.KnowledgeError("refused in simulation")

    monkeypatch.setattr(store_mod, "ingest_text", refuse)
    with pytest.raises(TaskFailure, match="knowledge store refused"):
        ingest_url_task({"url": "https://example.com/plain"}, _ctx(settings, "ingest_url"))

    with session_scope(settings.database_url) as session:
        count = session.execute(select(func.count()).select_from(Document)).scalar_one()
    assert count == 0, "a failed ingest must leave no document row"


# --- ask → knowledge grounding -----------------------------------------------------


def test_ask_grounded_by_real_sources(
    settings: Settings,
    _clean_knowledge_tables,
    gated_gateway,
) -> None:
    """An ask with stored matches must carry provenance for real chunks."""
    doc_id = _ingest_doc(
        settings,
        text="The quantum platypus nocturne hums at 42 hertz inside the synthflow lab.",
        title="Quantum Platypus Nocturne",
    )

    result = ask_task({"prompt": "quantum platypus nocturne"}, _ctx(settings))

    sources = result["sources"]
    assert sources, "matching knowledge must ground the answer"
    first = sources[0]
    assert first["document_id"] == doc_id
    assert first["retriever"] in ("hybrid", "lexical")
    assert first["score"] > 0

    with session_scope(settings.database_url) as session:
        chunk = session.get(Chunk, first["chunk_id"])
        assert chunk is not None, "every source must trace to a real chunk row"

    messages = gated_gateway["messages"]
    assert _FENCE_START in messages[0].content, "retrieved context stays fenced"
    assert "Quantum Platypus Nocturne" in messages[0].content

    events = _all_job_events(settings)
    used = [m for t, m in events if t == "knowledge_context_used"]
    assert used, "grounding must be observable in the event stream"
    assert used[-1]["hits"] == len(sources)


def test_ask_without_matches_is_explicitly_ungrounded(
    settings: Settings, _clean_knowledge_tables, gated_gateway
) -> None:
    """No retrieval hit means sources=[], never a fabricated citation."""
    result = ask_task({"prompt": "unrelated xylophone query"}, _ctx(settings))

    assert result["sources"] == []
    assert _FENCE_START not in gated_gateway["messages"][0].content


def test_ask_explicit_context_beats_auto_retrieval(
    settings: Settings, _clean_knowledge_tables, gated_gateway
) -> None:
    """An explicit context is used verbatim; no retrieval runs on top."""
    result = ask_task(
        {"prompt": "summarise", "context": "caller-provided context about zebras"},
        _ctx(settings),
    )

    assert result["sources"] == []
    messages = gated_gateway["messages"]
    assert "caller-provided context about zebras" in messages[0].content
    assert _FENCE_START in messages[0].content  # explicit context is fenced too
    events = _all_job_events(settings)
    assert not any(t == "knowledge_context_used" for t, _m in events)


def test_ask_knowledge_false_disables_retrieval(
    settings: Settings, _clean_knowledge_tables, gated_gateway
) -> None:
    _ingest_doc(settings, text="quantum platypus nocturne facts")

    result = ask_task({"prompt": "quantum platypus nocturne", "knowledge": False}, _ctx(settings))

    assert result["sources"] == []
    assert gated_gateway["messages"][0].content == "quantum platypus nocturne"


def test_ask_retrieval_failure_degrades_to_ungrounded(
    settings: Settings,
    _clean_knowledge_tables,
    gated_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retrieval breaking must yield an ungrounded answer and an event note."""
    import synapse.knowledge.search as search_mod

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("database exploded")

    monkeypatch.setattr(search_mod, "search_hybrid", boom)

    result = ask_task({"prompt": "quantum platypus nocturne"}, _ctx(settings))

    assert result["sources"] == []
    assert gated_gateway["messages"][0].content == "quantum platypus nocturne"
    events = _all_job_events(settings)
    unavailable = [(t, m) for t, m in events if t == "knowledge_context_unavailable"]
    assert unavailable, "the degraded grounding must be visible in events"
    assert "database exploded" in (unavailable[-1][1] or {}).get("reason", "")
