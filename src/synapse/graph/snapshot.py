"""Live system graph: real jobs, workers, documents, models as nodes.

This module is what makes the dashboard's "neural network" honest. Every node
and every edge returned here is traceable to a row in PostgreSQL. There are no
ambient particles, no decorative synapses: a node appears because a job was
created, an edge appears because a worker claimed that job or a document was
ingested by it.

Node kinds and their real provenance:

===========  ===========================================================
``job``      a row in ``jobs``; its status is the row's status
``worker``   a distinct ``jobs.worker_id`` that has claimed work
``document`` a row in ``documents`` (knowledge layer, ADR 0005)
``model``    a distinct ``provider/model_id`` seen in ``model_usage``
``chunk``    a row in ``chunks``, emitted only for recently touched docs
===========  ===========================================================

Edge kinds and their real provenance:

============  =========================================================
``claimed``   ``jobs.worker_id`` points at the worker
``used``      a ``model_usage`` row links job -> model
``produced``  an ``ingest_url`` job created the document
``contains``  ``chunks.document_id`` points at the document
============  =========================================================

**Bounding.** A naive "select everything" would emit one node per chunk, which
for a real corpus is hundreds of thousands of nodes and a browser that dies.
This module therefore bounds each node kind explicitly and reports what it
truncated in :class:`GraphSnapshot.truncated` — the dashboard shows "showing N
of M" rather than silently lying about the graph's true size.

**Payload safety.** Job payloads can contain user text and document content can
be arbitrary. Details are length-capped and passed through
:func:`synapse.security.redact` before leaving this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synapse.db.models import Job, ModelUsage
from synapse.security import redact

log = logging.getLogger("synapse.graph.snapshot")

# Bounds. Deliberately explicit: an unbounded graph is a denial-of-service on
# the browser that renders it.
MAX_JOB_NODES = 200
MAX_DOCUMENT_NODES = 120
MAX_CHUNK_NODES = 200
MAX_MODEL_NODES = 40
MAX_EDGES = 3000
# Per-node detail budget. Hover panels need context, not the whole document.
DETAIL_TEXT_LIMIT = 400
MAX_PAYLOAD_KEYS = 12

TERMINAL_JOB_STATUSES = ("completed", "failed", "cancelled", "expired")


@dataclass(frozen=True)
class GraphNode:
    """One real entity in the system, sized for rendering."""

    id: str
    kind: str  # job | worker | document | model | chunk
    label: str
    status: str | None = None
    group: str | None = None
    weight: float = 1.0
    created_at: datetime | None = None
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    """One real relationship between two entities."""

    source: str
    target: str
    kind: str  # claimed | used | produced | contains


@dataclass(frozen=True)
class GraphSnapshot:
    generated_at: datetime
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    counts: dict[str, int]
    truncated: dict[str, dict[str, int]]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def _as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive timestamp so arithmetic is never ambiguous."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _clip(text: str, limit: int = DETAIL_TEXT_LIMIT) -> str:
    """Bound free text; redact secrets before it can reach a browser."""
    bounded = redact(text or "")
    if bounded is None:
        return ""
    bounded = str(bounded)
    if len(bounded) <= limit:
        return bounded
    return bounded[: limit - 1] + "…"


def _job_details(job: Job) -> dict:
    """Everything a hover panel needs to explain one job honestly."""
    payload = dict(job.payload or {})
    # Only scalar payload keys are surfaced; nested blobs are clipped to text.
    # Strings pass through redact() and are length-capped: the dashboard must
    # never leak a secret from a payload or ship a 10k-char prompt verbatim.
    scalars = {}
    for key in sorted(payload)[:MAX_PAYLOAD_KEYS]:
        value = payload[key]
        if isinstance(value, str):
            scalars[key] = _clip(value)
        elif isinstance(value, int | float | bool):
            scalars[key] = value
        else:
            scalars[key] = _clip(str(value), 160)

    duration_ms: int | None = None
    if job.started_at and job.finished_at:
        started = _as_utc(job.started_at)
        finished = _as_utc(job.finished_at)
        duration_ms = int((finished - started).total_seconds() * 1000)

    return {
        "task_type": job.task_type,
        "status": job.status,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "current_step": job.current_step,
        "worker_id": job.worker_id,
        "correlation_id": job.correlation_id,
        "payload": scalars,
        "result": _clip(str(job.result), DETAIL_TEXT_LIMIT) if job.result else None,
        "error": _clip(job.error, DETAIL_TEXT_LIMIT) if job.error else None,
        "created_at": _iso(job.created_at),
        "started_at": _iso(job.started_at),
        "finished_at": _iso(job.finished_at),
        "duration_ms": duration_ms,
        "cancel_requested": job.cancel_requested,
    }


def _collect_jobs(
    session: Session, limit: int
) -> tuple[list[GraphNode], dict[str, GraphEdge], dict]:
    """Most recent jobs plus their worker and model edges."""
    rows = session.scalars(
        select(Job).order_by(Job.created_at.desc()).limit(limit)
    ).all()

    nodes: list[GraphNode] = []
    edges: dict[str, GraphEdge] = {}
    workers: dict[str, GraphNode] = {}
    models: dict[str, GraphNode] = {}

    for job in rows:
        nodes.append(
            GraphNode(
                id=f"job:{job.id}",
                kind="job",
                label=job.task_type,
                status=job.status,
                group=job.task_type,
                weight=2.0,
                created_at=job.created_at,
                details=_job_details(job),
            )
        )
        if job.worker_id:
            worker_id = f"worker:{job.worker_id}"
            if worker_id not in workers:
                workers[worker_id] = GraphNode(
                    id=worker_id,
                    kind="worker",
                    label=job.worker_id,
                    status="active" if job.status not in TERMINAL_JOB_STATUSES else "idle",
                    group="worker",
                    weight=3.0,
                    details={
                        "worker_id": job.worker_id,
                        "note": "Distinct worker identity observed on claimed jobs.",
                    },
                )
            edges[f"{job.id}->{worker_id}"] = GraphEdge(
                source=f"job:{job.id}", target=worker_id, kind="claimed"
            )

    # Model edges come from the usage ledger, not from job payloads: the
    # ledger is the durable record of what actually happened (ADR 0003).
    job_ids = [job.id for job in rows]
    if job_ids:
        usage_rows = session.execute(
            select(
                ModelUsage.job_id, ModelUsage.provider, ModelUsage.model_id, func.count()
            )
            .where(ModelUsage.job_id.in_(job_ids))
            .group_by(ModelUsage.job_id, ModelUsage.provider, ModelUsage.model_id)
        ).all()
        for usage_job_id, provider, model_id, calls in usage_rows:
            model_node_id = f"model:{provider}/{model_id}"
            if model_node_id not in models:
                models[model_node_id] = GraphNode(
                    id=model_node_id,
                    kind="model",
                    label=model_id,
                    group=provider,
                    weight=2.5,
                    details={"provider": provider, "model_id": model_id, "calls_in_view": 0},
                )
            # Accumulate real call counts across the jobs in this window, so a
            # hover panel reports how much work this model actually did.
            prior = models[model_node_id].details.get("calls_in_view", 0)
            models[model_node_id].details["calls_in_view"] = int(prior) + int(calls)
            edges[f"{usage_job_id}->{model_node_id}"] = GraphEdge(
                source=f"job:{usage_job_id}", target=model_node_id, kind="used"
            )

    nodes.extend(workers.values())
    nodes.extend(models.values())
    return nodes, edges, {"workers": len(workers), "models": len(models)}


def _collect_documents(
    session: Session, limit: int
) -> tuple[list[GraphNode], dict[str, GraphEdge], dict]:
    """Documents plus a bounded sample of their chunks."""
    from synapse.knowledge.models import Chunk as ChunkRow
    from synapse.knowledge.models import Document

    doc_rows = session.scalars(
        select(Document).order_by(Document.created_at.desc()).limit(limit)
    ).all()

    chunk_counts = dict(
        session.execute(
            select(ChunkRow.document_id, func.count())
            .where(ChunkRow.document_id.in_([d.id for d in doc_rows]))
            .group_by(ChunkRow.document_id)
        ).all()
    ) if doc_rows else {}

    nodes: list[GraphNode] = []
    edges: dict[str, GraphEdge] = {}
    for doc in doc_rows:
        node_id = f"doc:{doc.id}"
        nodes.append(
            GraphNode(
                id=node_id,
                kind="document",
                label=doc.title or doc.source,
                status="indexed" if doc.embedded_at else "pending",
                group=doc.source_type,
                weight=2.0,
                created_at=doc.created_at,
                details={
                    "source": _clip(doc.source, 300),
                    "source_type": doc.source_type,
                    "title": doc.title,
                    "char_count": doc.char_count,
                    "chunk_count": chunk_counts.get(doc.id, 0),
                    "content_type": doc.content_type,
                    "ingested_by": doc.ingested_by,
                    "embedded_at": _iso(doc.embedded_at),
                    "created_at": _iso(doc.created_at),
                    # Provenance hash, not content: the dashboard must not
                    # become a second copy of the corpus.
                    "content_hash": doc.content_hash[:16],
                },
            )
        )

    # A bounded chunk sample gives the graph something to animate when
    # knowledge is being written, without emitting one node per chunk.
    chunk_rows = session.execute(
        select(ChunkRow)
        .where(ChunkRow.document_id.in_([d.id for d in doc_rows]))
        .order_by(ChunkRow.created_at.desc())
        .limit(MAX_CHUNK_NODES)
    ).scalars().all()
    for chunk in chunk_rows:
        chunk_id = f"chunk:{chunk.id}"
        nodes.append(
            GraphNode(
                id=chunk_id,
                kind="chunk",
                label=f"chunk #{chunk.chunk_index}",
                status="indexed",
                group="chunk",
                weight=1.0,
                created_at=chunk.created_at,
                details={
                    "document_id": chunk.document_id,
                    "chunk_index": chunk.chunk_index,
                    "char_start": chunk.char_start,
                    "char_end": chunk.char_end,
                    "token_estimate": chunk.token_estimate,
                    "embedded": chunk.embedding is not None,
                    "preview": _clip(chunk.content, 220),
                },
            )
        )
        edges[f"{chunk.id}->{doc.id}"] = GraphEdge(
            source=chunk_id, target=f"doc:{chunk.document_id}", kind="contains"
        )

    return nodes, edges, {"documents": len(doc_rows), "chunks": len(chunk_rows)}


def build_graph(session: Session, *, now: datetime | None = None) -> GraphSnapshot:
    """Build one bounded snapshot of the real system graph."""
    now = now or datetime.now(UTC)
    recent_cutoff = now - timedelta(days=7)

    job_nodes, job_edges, _job_meta = _collect_jobs(session, MAX_JOB_NODES)
    doc_nodes, doc_edges, _doc_meta = _collect_documents(session, MAX_DOCUMENT_NODES)

    nodes = job_nodes + doc_nodes
    edges = list(job_edges.values()) + list(doc_edges.values())[:MAX_EDGES]

    # "Ingest produced this document": match on job result -> document id.
    produced = session.execute(
        select(Job.id, Job.result).where(
            Job.task_type == "ingest_url",
            Job.created_at >= recent_cutoff,
            Job.result.isnot(None),
        ).limit(MAX_DOCUMENT_NODES)
    ).all()
    known_doc_ids = {n.id for n in doc_nodes}
    for job_id, result in produced:
        document_id = (result or {}).get("document_id") if isinstance(result, dict) else None
        if document_id and f"doc:{document_id}" in known_doc_ids:
            edges.append(
                GraphEdge(source=f"job:{job_id}", target=f"doc:{document_id}", kind="produced")
            )

    node_ids = {n.id for n in nodes}
    edges = [e for e in edges if e.source in node_ids and e.target in node_ids]

    # True totals, so the dashboard can say "showing 200 of 4,318" instead of
    # implying the rendered subset is the whole system.
    from synapse.knowledge.models import Document as DocumentRow

    total_jobs = session.scalar(select(func.count()).select_from(Job)) or 0
    total_docs = session.scalar(select(func.count()).select_from(DocumentRow)) or 0

    counts = {
        "jobs": sum(1 for n in nodes if n.kind == "job"),
        "workers": sum(1 for n in nodes if n.kind == "worker"),
        "documents": sum(1 for n in nodes if n.kind == "document"),
        "chunks": sum(1 for n in nodes if n.kind == "chunk"),
        "models": sum(1 for n in nodes if n.kind == "model"),
        "edges": len(edges),
        "active": sum(
            1
            for n in nodes
            if n.kind == "job" and n.status not in TERMINAL_JOB_STATUSES
        ),
    }

    return GraphSnapshot(
        generated_at=now,
        nodes=nodes,
        edges=edges,
        counts=counts,
        truncated={
            "jobs": {"shown": counts["jobs"], "total": int(total_jobs), "cap": MAX_JOB_NODES},
            "documents": {
                "shown": counts["documents"],
                "total": int(total_docs),
                "cap": MAX_DOCUMENT_NODES,
            },
            "chunks": {"shown": counts["chunks"], "cap": MAX_CHUNK_NODES},
        },
    )
