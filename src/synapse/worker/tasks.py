"""Bounded task registry.

Tasks are small, deterministic, and safe: they receive a payload and a
``TaskContext`` and return a JSON-serialisable result. Tasks never touch the
network, the shell, or the filesystem directly — richer capabilities arrive
later behind permissioned tools (spec 12.3).

``TaskContext`` performs every database touch in its own short transaction so
that a long-running task never holds row locks that would block cancellation.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select

from synapse.config import Settings
from synapse.db import session_scope
from synapse.db.models import Job, utcnow
from synapse.events import emit as _emit


class TaskCancelled(Exception):
    """Raised inside a task when the job's cancel flag is observed."""


class TaskFailure(Exception):
    """Deliberate, user-visible task failure (bad input, unmet precondition)."""


@dataclass
class TaskContext:
    settings: Settings
    job_id: str
    correlation_id: str

    # --- short independent transactions, never the task's own -------------

    def heartbeat(self) -> None:
        deadline = utcnow() + timedelta(seconds=self.settings.job_lease_seconds)
        with session_scope(self.settings.database_url) as session:
            job = session.get(Job, self.job_id)
            if job is not None:
                job.lease_expires_at = deadline
                job.updated_at = utcnow()

    def should_cancel(self) -> bool:
        with session_scope(self.settings.database_url) as session:
            flag = session.execute(
                select(Job.cancel_requested).where(Job.id == self.job_id)
            ).scalar_one_or_none()
            return bool(flag)

    def ensure_not_cancelled(self) -> None:
        if self.should_cancel():
            raise TaskCancelled()

    def emit(self, event_type: str, **metadata: Any) -> None:
        with session_scope(self.settings.database_url) as session:
            _emit(
                session,
                job_id=self.job_id,
                event_type=event_type,
                component_id="task",
                correlation_id=self.correlation_id,
                metadata=metadata,
            )

    def sleep_cancellable(self, seconds: float) -> None:
        """Sleep in small slices, reacting to cancel requests and heartbeating."""
        steps = max(1, int(seconds * 10))
        for _ in range(steps):
            self.ensure_not_cancelled()
            time.sleep(seconds / steps)
            self.heartbeat()


# --- built-in Phase 1 tasks ---------------------------------------------------


def echo_task(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    text = payload.get("text")
    if not isinstance(text, str):
        raise TaskFailure("payload.text must be a string")
    ctx.ensure_not_cancelled()
    return {
        "text": text,
        "chars": len(text),
        "words": len(re.findall(r"\S+", text)),
        "received_at": utcnow().isoformat(),
    }


def text_stats_task(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise TaskFailure("payload.text must be a string")
    ctx.ensure_not_cancelled()
    words = re.findall(r"\S+", text)
    return {
        "chars": len(text),
        "bytes": len(text.encode("utf-8")),
        "words": len(words),
        "unique_words": len({w.lower() for w in words}),
        "lines": text.count("\n") + 1 if text else 0,
    }


def slow_task(payload: dict[str, Any], ctx: TaskContext) -> dict[str, Any]:
    """Cancellable long-running task used to exercise the job lifecycle."""
    raw = payload.get("seconds", 5)
    try:
        seconds = float(raw)
    except (TypeError, ValueError) as exc:
        raise TaskFailure("payload.seconds must be a number") from exc
    if not 0 <= seconds <= 60:
        raise TaskFailure("payload.seconds must be between 0 and 60")

    steps = max(1, int(seconds * 10))
    report_every = max(1, steps // 4)
    for index in range(steps):
        ctx.ensure_not_cancelled()
        time.sleep(seconds / steps)
        ctx.heartbeat()
        if index % report_every == 0:
            ctx.emit(
                "task_progress",
                step=index,
                total=steps,
                percent=int(index * 100 / steps),
            )
    ctx.ensure_not_cancelled()
    return {"slept_seconds": seconds, "steps": steps}


TaskFunc = Callable[[dict[str, Any], TaskContext], dict[str, Any]]

TASKS: dict[str, TaskFunc] = {
    "echo": echo_task,
    "text_stats": text_stats_task,
    "slow": slow_task,
}
