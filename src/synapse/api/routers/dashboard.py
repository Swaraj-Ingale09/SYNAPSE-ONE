"""Dashboard data routes: telemetry, live graph, and aggregate stats.

All handlers are synchronous and do their database work through
``session_scope``; FastAPI runs them in its threadpool, so the event loop is
never blocked by a query or by an ``nvidia-smi`` subprocess spawn.

Every value returned here was measured or read from a row. Nothing is
synthesised for appearance: when the host has no GPU, ``gpu.available`` is
``false`` with a reason; when no job has finished, ``success_rate_24h`` is
``null`` rather than ``0.0``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from synapse.api.dashboard_contracts import (
    ActivityOut,
    DiskOut,
    GpuOut,
    GraphEdgeOut,
    GraphNodeOut,
    GraphOut,
    JobStatsOut,
    MemoryOut,
    ModelRollupOut,
    NetworkOut,
    TelemetryOut,
    TruncationOut,
    UsageOut,
)
from synapse.db import session_scope
from synapse.graph import build_graph
from synapse.stats import activity_stats, job_stats, usage_stats
from synapse.telemetry.system import GpuSample, get_collector

log = logging.getLogger("synapse.api.dashboard")

router = APIRouter()


def _gpu_out(sample: object) -> GpuOut:
    """Convert a telemetry GPU result, preserving unavailability honestly."""
    if isinstance(sample, GpuSample):
        return GpuOut(
            available=True,
            name=sample.name,
            index=sample.index,
            utilization_percent=sample.utilization_percent,
            memory_used_mb=sample.memory_used_mb,
            memory_total_mb=sample.memory_total_mb,
            memory_percent=sample.memory_percent,
            temperature_c=sample.temperature_c,
        )
    return GpuOut(available=False, reason=getattr(sample, "reason", "unknown"))


@router.get("/dashboard/telemetry", response_model=TelemetryOut)
def telemetry() -> TelemetryOut:
    """One live reading of the host (CPU, memory, disk, network, GPU)."""
    snapshot = get_collector().snapshot()
    network = (
        NetworkOut(
            bytes_sent_per_sec=snapshot.network.bytes_sent_per_sec,
            bytes_recv_per_sec=snapshot.network.bytes_recv_per_sec,
            sample_age_seconds=snapshot.network.sample_age_seconds,
            total_bytes_sent=snapshot.network.total_bytes_sent,
            total_bytes_recv=snapshot.network.total_bytes_recv,
        )
        if snapshot.network
        else None
    )
    return TelemetryOut(
        timestamp=snapshot.timestamp,
        cpu_percent=snapshot.cpu_percent,
        cpu_count_logical=snapshot.cpu_count_logical,
        cpu_count_physical=snapshot.cpu_count_physical,
        load_average_1m=snapshot.load_average_1m,
        load_average_unavailable_reason=snapshot.load_average_unavailable_reason,
        memory=MemoryOut(
            total_bytes=snapshot.memory.total_bytes,
            available_bytes=snapshot.memory.available_bytes,
            used_bytes=snapshot.memory.used_bytes,
            percent=snapshot.memory.percent,
        ),
        swap_percent=snapshot.swap_percent,
        swap_unavailable_reason=snapshot.swap_unavailable_reason,
        disk=DiskOut(
            total_bytes=snapshot.disk.total_bytes,
            used_bytes=snapshot.disk.used_bytes,
            free_bytes=snapshot.disk.free_bytes,
            percent=snapshot.disk.percent,
            mount=snapshot.disk.mount,
        ),
        network=network,
        gpu=_gpu_out(snapshot.gpu),
        process_uptime_seconds=snapshot.process_uptime_seconds,
        process_rss_bytes=snapshot.process_rss_bytes,
        process_threads=snapshot.process_threads,
    )


@router.get("/dashboard/graph", response_model=GraphOut)
def graph(request: Request) -> GraphOut:
    """The live system graph: real jobs, workers, documents, and models.

    This is the data behind the dashboard's "neural network". Nodes and edges
    are read from PostgreSQL on every request, so what is rendered is what is
    true right now — not a cached or decorative animation.
    """
    settings = request.app.state.settings
    with session_scope(settings.database_url) as session:
        snapshot = build_graph(session)

    return GraphOut(
        generated_at=snapshot.generated_at,
        nodes=[
            GraphNodeOut(
                id=node.id,
                kind=node.kind,
                label=node.label,
                status=node.status,
                group=node.group,
                weight=node.weight,
                created_at=node.created_at,
                details=node.details,
            )
            for node in snapshot.nodes
        ],
        edges=[
            GraphEdgeOut(source=edge.source, target=edge.target, kind=edge.kind)
            for edge in snapshot.edges
        ],
        counts=snapshot.counts,
        truncated={
            key: TruncationOut(
                shown=value["shown"],
                total=value.get("total"),
                cap=value["cap"],
            )
            for key, value in snapshot.truncated.items()
        },
    )


@router.get("/dashboard/stats", response_model=JobStatsOut)
def stats(request: Request) -> JobStatsOut:
    settings = request.app.state.settings
    with session_scope(settings.database_url) as session:
        result = job_stats(session)
    return JobStatsOut(
        total=result.total,
        by_status=result.by_status,
        active=result.active,
        queued=result.queued,
        succeeded_24h=result.succeeded_24h,
        failed_24h=result.failed_24h,
        success_rate_24h=result.success_rate_24h,
        throughput_per_hour_24h=result.throughput_per_hour_24h,
    )


@router.get("/dashboard/usage", response_model=UsageOut)
def usage(request: Request) -> UsageOut:
    settings = request.app.state.settings
    with session_scope(settings.database_url) as session:
        result = usage_stats(session)
    return UsageOut(
        total_calls=result.total_calls,
        succeeded=result.succeeded,
        failed=result.failed,
        total_input_tokens=result.total_input_tokens,
        total_output_tokens=result.total_output_tokens,
        total_cost_usd=result.total_cost_usd,
        priced_calls=result.priced_calls,
        unknown_price_calls=result.unknown_price_calls,
        avg_latency_ms=result.avg_latency_ms,
        models=[
            ModelRollupOut(
                provider=model.provider,
                model_id=model.model_id,
                calls=model.calls,
                input_tokens=model.input_tokens,
                output_tokens=model.output_tokens,
                total_cost_usd=model.total_cost_usd,
                avg_latency_ms=model.avg_latency_ms,
                success_rate=model.success_rate,
                last_used_at=model.last_used_at,
            )
            for model in result.models
        ],
    )


@router.get("/dashboard/activity", response_model=ActivityOut)
def activity(request: Request) -> ActivityOut:
    settings = request.app.state.settings
    with session_scope(settings.database_url) as session:
        result = activity_stats(session)
    return ActivityOut(
        active_hours_7d=result.active_hours_7d,
        active_days_7d=result.active_days_7d,
        total_runtime_seconds_7d=result.total_runtime_seconds_7d,
        first_event_at=result.first_event_at,
        last_event_at=result.last_event_at,
        domains=result.domains,
        source_types=result.source_types,
    )
