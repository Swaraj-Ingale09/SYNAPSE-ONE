"""Dashboard backend: telemetry, graph builder, and stats rollups.

These tests assert the *honesty* guarantees, not merely that routes return 200:

- a node in the graph must correspond to a row that actually exists;
- ``success_rate_24h`` must be ``None`` (not ``0.0``) when nothing has finished,
  because "no verdict yet" and "everything failed" are different facts;
- an unavailable measurement (no GPU, no load average on Windows) must be
  reported as unavailable, never as a measured zero;
- truncation must be reported so the UI can say "showing X of Y".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from synapse.db.models import Job, ModelUsage, utcnow
from synapse.events import emit
from synapse.graph import build_graph
from synapse.stats import activity_stats, job_stats, usage_stats
from synapse.telemetry.system import GpuSample, GpuUnavailable, TelemetryCollector

# --- telemetry: measured, and honest about what cannot be measured --------------


def test_collector_returns_real_cpu_and_memory() -> None:
    collector = TelemetryCollector()
    snap = collector.snapshot()
    # psutil on any supported host returns a plausible percentage; the point is
    # that it is a number, not None, because CPU is always measurable.
    assert isinstance(snap.cpu_percent, float)
    assert 0.0 <= snap.cpu_percent <= 100.0
    assert snap.memory.total_bytes > 0
    assert 0.0 <= snap.memory.percent <= 100.0
    assert snap.disk.total_bytes > 0


def test_network_rate_is_none_until_two_samples() -> None:
    """A rate needs a time base; with one sample we must refuse to invent one."""
    collector = TelemetryCollector()
    first = collector.snapshot()
    assert first.network is None, "one sample cannot yield a rate"

    second = collector.snapshot()
    # The second snapshot appends a second sample, so a rate may now exist.
    if second.network is not None:
        assert second.network.bytes_recv_per_sec >= 0.0
        assert second.network.sample_age_seconds >= 0.0


def test_gpu_unavailable_carries_a_reason_not_a_zero() -> None:
    """A missing GPU must not be reported as a measured 0% utilization."""
    # A bogus path (not None — None auto-detects the real GPU on this machine)
    # forces the failed-execution branch deterministically.
    collector = TelemetryCollector(
        nvidia_smi_path=r"C:\definitely\not\a\real\nvidia-smi.exe"
    )
    result = collector._read_gpu()  # noqa: SLF001 - deliberate direct check
    assert isinstance(result, GpuUnavailable)
    assert result.reason, "unavailability must state why"


def test_parse_handles_supported_and_virtualised_gpus() -> None:
    """Both real and '[Not Supported]' nvidia-smi rows parse without crashing."""
    from synapse.telemetry.system import parse_nvidia_smi_output

    real = parse_nvidia_smi_output(
        "0, NVIDIA GeForce RTX 4060 Laptop GPU, 42, 1024, 8192, 61\n"
    )
    assert isinstance(real, GpuSample)
    assert real.name == "NVIDIA GeForce RTX 4060 Laptop GPU"
    assert real.utilization_percent == 42.0
    assert real.temperature_c == 61.0
    assert real.memory_percent == pytest.approx(12.5)

    # Virtualised / MSHybrid GPUs report "[Not Supported]" for temperature.
    hybrid = parse_nvidia_smi_output("0, Quadro P1000, 0, 0, 4096, [Not Supported]\n")
    assert isinstance(hybrid, GpuSample)
    assert hybrid.temperature_c is None

    # Garbage input must be reported unavailable, never fabricate a sample.
    junk = parse_nvidia_smi_output("no gpu data here at all\n\n")
    assert isinstance(junk, GpuUnavailable)
    assert junk.reason


# --- graph builder: every node traces to a row ----------------------------------


def _make_job(session, *, status: str = "queued", worker_id: str | None = None) -> Job:
    job = Job(
        id=str(uuid.uuid4()),
        task_type="ask",
        status=status,
        payload={"prompt": "test prompt"},
        correlation_id=str(uuid.uuid4()),
        worker_id=worker_id,
        attempts=1 if status != "queued" else 0,
        created_at=utcnow(),
    )
    session.add(job)
    session.flush()
    emit(
        session,
        job_id=job.id,
        event_type=f"job_{status}",
        component_id="worker",
        status=status,
        correlation_id=job.correlation_id,
    )
    return job


def test_graph_nodes_correspond_to_real_rows(settings, _clean_knowledge_tables) -> None:
    from synapse.db import session_scope
    from synapse.db.models import Job as JobRow

    with session_scope(settings.database_url) as session:
        job = _make_job(session, status="running", worker_id="host:1")
        snapshot = build_graph(session)

    job_nodes = [n for n in snapshot.nodes if n.kind == "job"]
    assert job_nodes, "the job we created must appear"
    node = next(n for n in job_nodes if n.id == f"job:{job.id}")
    assert node.status == "running"
    assert node.details["task_type"] == "ask"
    assert node.details["payload"]["prompt"] == "test prompt"

    # The worker edge must exist because worker_id was set on the row.
    worker_ids = {e.target for e in snapshot.edges if e.source == node.id and e.kind == "claimed"}
    assert worker_ids == {"worker:host:1"}

    # And the row really is there.
    assert session.get(JobRow, job.id) is not None


def test_graph_reports_truncation_with_true_totals(
    settings, _clean_knowledge_tables
) -> None:
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        for _ in range(3):
            _make_job(session)
        snapshot = build_graph(session)

    trunc = snapshot.truncated["jobs"]
    assert trunc["total"] >= 3, "total must reflect the database, not the render cap"
    assert trunc["shown"] == snapshot.counts["jobs"]
    assert trunc["cap"] >= trunc["shown"]


def test_queued_job_has_no_claimed_edge(settings, _clean_knowledge_tables) -> None:
    """An unclaimed job must not be drawn as if a worker owned it."""
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        job = _make_job(session, status="queued", worker_id=None)
        snapshot = build_graph(session)

    claimed = [e for e in snapshot.edges if e.source == f"job:{job.id}" and e.kind == "claimed"]
    assert claimed == []


def test_graph_details_are_length_bounded(settings, _clean_knowledge_tables) -> None:
    """A huge payload must be clipped, not shipped whole to the browser."""
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        job = Job(
            id=str(uuid.uuid4()),
            task_type="ask",
            status="queued",
            payload={"prompt": "x" * 10_000},
            correlation_id=str(uuid.uuid4()),
            attempts=0,
            created_at=utcnow(),
        )
        session.add(job)
        session.flush()
        snapshot = build_graph(session)

    node = next(n for n in snapshot.nodes if n.id == f"job:{job.id}")
    prompt = node.details["payload"]["prompt"]
    assert len(prompt) <= 400, f"payload was not clipped: {len(prompt)} chars"


def test_graph_redacts_secrets_in_details(settings, _clean_knowledge_tables) -> None:
    """A payload containing a key must not leak it to the dashboard."""
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        job = Job(
            id=str(uuid.uuid4()),
            task_type="ask",
            status="queued",
            payload={"prompt": "use api_key=sk-abcdefghijklmnop for the service"},
            correlation_id=str(uuid.uuid4()),
            attempts=0,
            created_at=utcnow(),
        )
        session.add(job)
        session.flush()
        snapshot = build_graph(session)

    node = next(n for n in snapshot.nodes if n.id == f"job:{job.id}")
    assert "sk-abcdefghijklmnop" not in str(node.details)
    assert "<redacted>" in str(node.details)


# --- stats: null is not zero ----------------------------------------------------


def test_success_rate_is_none_when_nothing_finished(
    settings, _clean_knowledge_tables
) -> None:
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        _make_job(session, status="queued")
        stats = job_stats(session)

    # Nothing has reached a verdict, so a rate would be fabrication.
    assert stats.success_rate_24h is None
    assert stats.succeeded_24h == 0
    assert stats.failed_24h == 0


def test_success_rate_is_real_once_jobs_finish(settings, _clean_knowledge_tables) -> None:
    from synapse.db import session_scope

    now = utcnow()
    with session_scope(settings.database_url) as session:
        for status in ("completed", "completed", "failed"):
            job = _make_job(session, status=status)
            row = session.get(Job, job.id)
            row.started_at = now - timedelta(seconds=5)
            row.finished_at = now
        stats = job_stats(session)

    assert stats.success_rate_24h == pytest.approx(2 / 3)
    assert stats.succeeded_24h == 2
    assert stats.failed_24h == 1


def test_usage_stats_empty_ledger_reports_no_cost(
    settings, _clean_knowledge_tables
) -> None:
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        usage = usage_stats(session)

    # No calls means cost is unknown, not "free".
    assert usage.total_calls == 0
    assert usage.total_cost_usd is None
    assert usage.models == []


def test_usage_stats_aggregates_real_ledger_rows(settings, _clean_knowledge_tables) -> None:
    from synapse.db import session_scope

    with session_scope(settings.database_url) as session:
        for i in range(3):
            session.add(
                ModelUsage(
                    provider="local",
                    model_id="mimo-v2.6-flash-free",
                    input_tokens=100,
                    output_tokens=50,
                    latency_ms=200,
                    status="succeeded" if i < 2 else "failed",
                    price_known=False,
                )
            )
        usage = usage_stats(session)

    assert usage.total_calls == 3
    assert usage.succeeded == 2
    assert usage.failed == 1
    assert usage.total_input_tokens == 300
    # Every call lacked a price, so the total must stay unknown rather than 0.
    assert usage.total_cost_usd is None
    assert usage.unknown_price_calls == 3

    model = usage.models[0]
    assert model.model_id == "mimo-v2.6-flash-free"
    assert model.calls == 3
    assert model.success_rate == pytest.approx(2 / 3)


def test_activity_stats_counts_distinct_active_hours(
    settings, _clean_knowledge_tables
) -> None:
    """Events anchored to two distinct hours yield >= 2 active hours."""
    from datetime import UTC, datetime

    from synapse.db import session_scope
    from synapse.db.models import JobEvent

    now = datetime.now(UTC)
    # Two timestamps guaranteed to sit in different clock hours: floor the
    # current minute and go two hours back. Naive offsets like
    # "now - timedelta(minutes=5)" cannot guarantee that.
    hour_a = now.replace(minute=2, second=0, microsecond=0)
    hour_b = hour_a - timedelta(hours=2)

    with session_scope(settings.database_url) as session:
        job = _make_job(session, status="queued")
        for stamp in (hour_a, hour_b):
            session.add(
                JobEvent(
                    job_id=job.id,
                    event_type="probe",
                    component_id="test",
                    status="succeeded",
                    correlation_id=job.correlation_id,
                    timestamp=stamp,
                )
            )
        session.flush()
        activity = activity_stats(session)

    assert activity.active_hours_7d >= 2
    assert activity.active_days_7d >= 1
    assert activity.first_event_at is not None
    assert activity.last_event_at is not None


# --- API routes -------------------------------------------------------------------


def test_dashboard_routes_require_token(client) -> None:
    for path in (
        "/v1/dashboard/telemetry",
        "/v1/dashboard/graph",
        "/v1/dashboard/stats",
        "/v1/dashboard/usage",
        "/v1/dashboard/activity",
    ):
        assert client.get(path).status_code == 401, f"{path} must be guarded"


def test_tlemetry_route_returns_measured_values(client, auth) -> None:
    response = client.get("/v1/dashboard/telemetry", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert 0.0 <= body["cpu_percent"] <= 100.0
    assert body["memory"]["total_bytes"] > 0
    # GPU must be an explicit available/unavailable decision, never absent.
    assert isinstance(body["gpu"]["available"], bool)
    if not body["gpu"]["available"]:
        assert body["gpu"]["reason"], "an unavailable GPU must say why"


def test_graph_route_shape_matches_contract(client, auth) -> None:
    response = client.get("/v1/dashboard/graph", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"generated_at", "nodes", "edges", "counts", "truncated"}
    for node in body["nodes"]:
        assert set(node) == {
            "id", "kind", "label", "status", "group", "weight", "created_at", "details",
        }
        assert node["kind"] in {"job", "worker", "document", "model", "chunk"}
    for edge in body["edges"]:
        assert set(edge) == {"source", "target", "kind"}
        assert edge["kind"] in {"claimed", "used", "produced", "contains"}


def test_stats_route_distinguishes_null_success_rate(client, auth) -> None:
    body = client.get("/v1/dashboard/stats", headers=auth).json()
    # The test DB is cleaned between tests, so nothing has finished: this must
    # serialise as JSON null, not 0.0.
    assert body["success_rate_24h"] is None
    assert body["total"] == 0


def test_usage_route_empty_ledger_is_honest(client, auth) -> None:
    body = client.get("/v1/dashboard/usage", headers=auth).json()
    assert body["total_calls"] == 0
    assert body["total_cost_usd"] is None
    assert body["models"] == []
