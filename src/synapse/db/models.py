"""SQLAlchemy models: durable jobs and their event trail (spec 9.2, 14.3)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import BIGINT, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('created','queued','running','waiting_for_approval',"
            "'waiting_for_input','retrying','completed','failed','cancelled','expired')",
            name="ck_jobs_status",
        ),
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    project_scope: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_scope: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    correlation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_step: Mapped[str | None] = mapped_column(String(128), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list[JobEvent]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="JobEvent.id",
        passive_deletes=True,
    )


class JobEvent(Base):
    """One row per observable job lifecycle / worker event."""

    __tablename__ = "job_events"
    __table_args__ = (Index("ix_job_events_job_id_id", "job_id", "id"),)

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    component_id: Mapped[str] = mapped_column(String(64), nullable=False, default="core")
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # "metadata" is a reserved attribute name on the declarative base; map explicitly.
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

    job: Mapped[Job] = relationship(back_populates="events")


class ModelUsage(Base):
    """Durable ledger of every model-gateway request (spec 10.4, ADR 0003).

    One row per provider call, written by the gateway after the call returns.
    Token counts are always the provider's own reported figures; ``cost_usd``
    is NULL when the model has no known price ("unknown", never a guessed 0),
    so cost can always be recomputed once a price is supplied.
    """

    __tablename__ = "model_usage"
    __table_args__ = (
        CheckConstraint(
            "status IN ('succeeded','failed')",
            name="ck_model_usage_status",
        ),
        CheckConstraint(
            "input_tokens >= 0 AND output_tokens >= 0",
            name="ck_model_usage_tokens_nonneg",
        ),
        Index("ix_model_usage_created_at", "created_at"),
        Index("ix_model_usage_provider_model", "provider", "model_id"),
    )

    id: Mapped[int] = mapped_column(BIGINT, primary_key=True, autoincrement=True)
    job_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("jobs.id", ondelete="SET NULL"),
        nullable=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    alias: Mapped[str | None] = mapped_column(String(128), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Numeric, not float: money must not accumulate binary floating-point error.
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    price_known: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="succeeded")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
