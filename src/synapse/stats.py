"""Aggregate system statistics for the dashboard's side panels.

Everything here is computed from rows that exist. Where the underlying data
does not exist yet, the field reports ``None`` with a reason rather than
``0`` — an empty system and a broken measurement look identical on a
dashboard if both render as zero, so they are kept distinguishable.

Three groups:

- :func:`job_stats` — job counts, throughput, and success rate
- :func:`usage_stats` — the model-usage ledger rolled up per model
- :func:`activity_stats` — real "working hours" and "domains worked"

**On "working hours".** There is no attendance clock in this system, so this
is not wall-clock hours a human was present. It is *system activity*: distinct
hours in which at least one job event was written, plus the summed duration of
jobs that actually ran. Both are labelled precisely in the payload so the
dashboard cannot be misread as tracking a person.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from synapse.db.models import Job, JobEvent, ModelUsage

log = logging.getLogger("synapse.stats")

# Rolling windows the dashboard cares about.
WINDOW_24H = timedelta(hours=24)
WINDOW_7D = timedelta(days=7)
# Cap the per-domain breakdown so one system with thousands of task types
# cannot produce an unbounded payload.
MAX_DOMAIN_BUCKETS = 24


@dataclass(frozen=True)
class JobStats:
    total: int
    by_status: dict[str, int]
    active: int
    queued: int
    succeeded_24h: int
    failed_24h: int
    # None when no job has ever finished: a success *rate* of 0.0 would be a
    # lie, because it would claim every attempt failed.
    success_rate_24h: float | None
    throughput_per_hour_24h: float


def job_stats(session: Session, *, now: datetime | None = None) -> JobStats:
    now = now or datetime.now(UTC)
    since = now - WINDOW_24H

    rows = session.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    by_status = {status: int(count) for status, count in rows}
    total = sum(by_status.values())
    active_statuses = (
        "created",
        "queued",
        "running",
        "retrying",
        "waiting_for_approval",
        "waiting_for_input",
    )
    active = sum(count for status, count in by_status.items() if status in active_statuses)
    queued = by_status.get("queued", 0)

    finished_recent = session.execute(
        select(Job.status, func.count())
        .where(Job.finished_at.isnot(None), Job.finished_at >= since)
        .group_by(Job.status)
    ).all()
    recent_map = {status: int(count) for status, count in finished_recent}
    succeeded = recent_map.get("completed", 0)
    failed = recent_map.get("failed", 0)
    decided = succeeded + failed

    started_recent = session.scalar(
        select(func.count()).select_from(Job).where(Job.created_at >= since)
    ) or 0

    return JobStats(
        total=total,
        by_status=by_status,
        active=active,
        queued=queued,
        succeeded_24h=succeeded,
        failed_24h=failed,
        # Only meaningful once at least one job has actually reached a verdict.
        success_rate_24h=(succeeded / decided) if decided > 0 else None,
        throughput_per_hour_24h=round(started_recent / 24.0, 3),
    )


@dataclass(frozen=True)
class ModelRollup:
    provider: str
    model_id: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_cost_usd: float | None
    avg_latency_ms: float
    success_rate: float
    last_used_at: datetime | None


@dataclass(frozen=True)
class UsageStats:
    total_calls: int
    succeeded: int
    failed: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float | None
    # None when no priced call exists; a 0.0 would assert "free" falsely.
    priced_calls: int
    unknown_price_calls: int
    avg_latency_ms: float | None
    models: list[ModelRollup]


def usage_stats(session: Session, *, limit: int = 12) -> UsageStats:
    total_calls = session.scalar(select(func.count()).select_from(ModelUsage)) or 0
    if total_calls == 0:
        return UsageStats(
            total_calls=0,
            succeeded=0,
            failed=0,
            total_input_tokens=0,
            total_output_tokens=0,
            total_cost_usd=None,
            priced_calls=0,
            unknown_price_calls=0,
            avg_latency_ms=None,
            models=[],
        )

    status_rows = session.execute(
        select(ModelUsage.status, func.count()).group_by(ModelUsage.status)
    ).all()
    status_map = {status: int(count) for status, count in status_rows}
    succeeded = status_map.get("succeeded", 0)
    failed = status_map.get("failed", 0)

    totals = session.execute(
        select(
            func.coalesce(func.sum(ModelUsage.input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.output_tokens), 0),
            func.coalesce(func.sum(ModelUsage.cost_usd), 0.0),
            func.count(ModelUsage.id).filter(ModelUsage.price_known),
            func.count(ModelUsage.id).filter(~ModelUsage.price_known),
            func.avg(ModelUsage.latency_ms),
        )
    ).one()

    per_model = session.execute(
        select(
            ModelUsage.provider,
            ModelUsage.model_id,
            func.count(ModelUsage.id),
            func.coalesce(func.sum(ModelUsage.input_tokens), 0),
            func.coalesce(func.sum(ModelUsage.output_tokens), 0),
            func.sum(ModelUsage.cost_usd),
            func.avg(ModelUsage.latency_ms),
            func.count(ModelUsage.id).filter(ModelUsage.status == "succeeded"),
            func.max(ModelUsage.created_at),
        )
        .group_by(ModelUsage.provider, ModelUsage.model_id)
        .order_by(func.count(ModelUsage.id).desc())
        .limit(limit)
    ).all()

    models = []
    for provider, model_id, calls, in_tok, out_tok, cost, latency, ok, last_at in per_model:
        models.append(
            ModelRollup(
                provider=provider,
                model_id=model_id,
                calls=int(calls),
                input_tokens=int(in_tok),
                output_tokens=int(out_tok),
                # Cost stays None while *every* call for this model lacked a
                # price; showing 0.0 would claim the tokens were free.
                total_cost_usd=float(cost) if cost is not None else None,
                avg_latency_ms=float(latency or 0.0),
                success_rate=(int(ok) / int(calls)) if calls else 0.0,
                last_used_at=last_at,
            )
        )

    priced = int(totals[3])
    return UsageStats(
        total_calls=total_calls,
        succeeded=succeeded,
        failed=failed,
        total_input_tokens=int(totals[0]),
        total_output_tokens=int(totals[1]),
        total_cost_usd=float(totals[2]) if priced > 0 else None,
        priced_calls=priced,
        unknown_price_calls=int(totals[4]),
        avg_latency_ms=float(totals[5]) if totals[5] is not None else None,
        models=models,
    )


@dataclass(frozen=True)
class ActivityStats:
    # Distinct hours in which at least one job event was written.
    active_hours_7d: int
    # Distinct calendar days with activity.
    active_days_7d: int
    # Summed runtime of jobs that actually finished, in seconds.
    total_runtime_seconds_7d: float | None
    first_event_at: datetime | None
    last_event_at: datetime | None
    # Task-type distribution: the real "domains worked".
    domains: dict[str, int]
    # Source-type distribution across ingested documents.
    source_types: dict[str, int]


def activity_stats(session: Session, *, now: datetime | None = None) -> ActivityStats:
    now = now or datetime.now(UTC)
    since = now - WINDOW_7D

    # Postgres date_trunc on the event timestamp is the honest way to count
    # distinct active hours; doing it in Python would need every row in memory.
    hour_rows = session.execute(
        select(func.count(func.distinct(func.date_trunc("hour", JobEvent.timestamp)))).where(
            JobEvent.timestamp >= since
        )
    ).scalar()
    day_rows = session.execute(
        select(func.count(func.distinct(func.date_trunc("day", JobEvent.timestamp)))).where(
            JobEvent.timestamp >= since
        )
    ).scalar()

    runtime = session.execute(
        select(
            func.sum(
                func.extract(
                    "epoch",
                    func.coalesce(Job.finished_at, now) - func.coalesce(Job.started_at, now),
                )
            )
        ).where(Job.finished_at.isnot(None), Job.finished_at >= since)
    ).scalar()

    first_at = session.scalar(select(func.min(JobEvent.timestamp)))
    last_at = session.scalar(select(func.max(JobEvent.timestamp)))

    domain_rows = session.execute(
        select(Job.task_type, func.count())
        .where(Job.created_at >= since)
        .group_by(Job.task_type)
        .order_by(func.count().desc())
        .limit(MAX_DOMAIN_BUCKETS)
    ).all()

    from synapse.knowledge.models import Document

    source_rows = session.execute(
        select(Document.source_type, func.count()).group_by(Document.source_type)
    ).all()

    return ActivityStats(
        active_hours_7d=int(hour_rows or 0),
        active_days_7d=int(day_rows or 0),
        total_runtime_seconds_7d=float(runtime) if runtime is not None else None,
        first_event_at=first_at,
        last_event_at=last_at,
        domains={task: int(count) for task, count in domain_rows},
        source_types={stype: int(count) for stype, count in source_rows},
    )
