"""Contracts for the live dashboard: telemetry, graph, and aggregate stats.

These are the wire shapes the React dashboard consumes. They mirror the
*measured* values from :mod:`synapse.telemetry`, :mod:`synapse.graph`, and
:mod:`synapse.stats` — notably ``None`` is preserved rather than coerced to
``0``, so "not measured" stays distinguishable from "measured as zero" in the
UI.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class MemoryOut(BaseModel):
    total_bytes: int
    available_bytes: int
    used_bytes: int
    percent: float


class DiskOut(BaseModel):
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float
    mount: str


class NetworkOut(BaseModel):
    bytes_sent_per_sec: float
    bytes_recv_per_sec: float
    sample_age_seconds: float
    total_bytes_sent: int
    total_bytes_recv: int


class GpuOut(BaseModel):
    """Either a real reading or an explicit statement that none is possible."""

    available: bool
    name: str | None = None
    index: int | None = None
    utilization_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    memory_percent: float | None = None
    temperature_c: float | None = None
    reason: str | None = None


class TelemetryOut(BaseModel):
    timestamp: datetime
    cpu_percent: float
    cpu_count_logical: int
    cpu_count_physical: int | None
    load_average_1m: float | None
    load_average_unavailable_reason: str | None
    memory: MemoryOut
    swap_percent: float | None
    swap_unavailable_reason: str | None
    disk: DiskOut
    network: NetworkOut | None
    gpu: GpuOut
    process_uptime_seconds: float
    process_rss_bytes: float
    process_threads: int


class GraphNodeOut(BaseModel):
    id: str
    kind: Literal["job", "worker", "document", "model", "chunk"]
    label: str
    status: str | None = None
    group: str | None = None
    weight: float = 1.0
    created_at: datetime | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class GraphEdgeOut(BaseModel):
    source: str
    target: str
    kind: Literal["claimed", "used", "produced", "contains"]


class TruncationOut(BaseModel):
    shown: int
    total: int | None = None
    cap: int


class GraphOut(BaseModel):
    generated_at: datetime
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    counts: dict[str, int]
    truncated: dict[str, TruncationOut]


class JobStatsOut(BaseModel):
    total: int
    by_status: dict[str, int]
    active: int
    queued: int
    succeeded_24h: int
    failed_24h: int
    # None means "no job has finished yet", not "zero percent succeeded".
    success_rate_24h: float | None
    throughput_per_hour_24h: float


class ModelRollupOut(BaseModel):
    provider: str
    model_id: str
    calls: int
    input_tokens: int
    output_tokens: int
    # None means no call for this model has a known price.
    total_cost_usd: float | None
    avg_latency_ms: float
    success_rate: float
    last_used_at: datetime | None


class UsageOut(BaseModel):
    total_calls: int
    succeeded: int
    failed: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float | None
    priced_calls: int
    unknown_price_calls: int
    avg_latency_ms: float | None
    models: list[ModelRollupOut]


class ActivityOut(BaseModel):
    """Real activity aggregates for the side panel.

    ``active_hours_7d`` counts distinct hours in which at least one job event
    was written. It measures *system activity*, not a person's attendance.
    """

    active_hours_7d: int
    active_days_7d: int
    total_runtime_seconds_7d: float | None
    first_event_at: datetime | None
    last_event_at: datetime | None
    domains: dict[str, int]
    source_types: dict[str, int]
