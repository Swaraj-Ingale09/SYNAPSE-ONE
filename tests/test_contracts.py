"""State-machine contract tests (spec 9.2): no database required."""

from __future__ import annotations

import pytest

from synapse.contracts import (
    LEGAL_TRANSITIONS,
    TERMINAL_STATES,
    InvalidTransition,
    JobStatus,
    assert_transition,
)


def test_terminal_states_have_no_exits() -> None:
    for state in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[state] == frozenset()


def test_queued_can_become_running_and_cancelled() -> None:
    assert JobStatus.RUNNING in LEGAL_TRANSITIONS[JobStatus.QUEUED]
    assert JobStatus.CANCELLED in LEGAL_TRANSITIONS[JobStatus.QUEUED]


def test_running_can_reach_all_executing_outcomes() -> None:
    for target in (
        JobStatus.COMPLETED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.RETRYING,
    ):
        assert target in LEGAL_TRANSITIONS[JobStatus.RUNNING]


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (JobStatus.COMPLETED, JobStatus.RUNNING),
        (JobStatus.CANCELLED, JobStatus.QUEUED),
        (JobStatus.FAILED, JobStatus.COMPLETED),
        (JobStatus.QUEUED, JobStatus.COMPLETED),
        (JobStatus.RETRYING, JobStatus.RUNNING),
    ],
)
def test_illegal_transitions_raise(current: JobStatus, new: JobStatus) -> None:
    with pytest.raises(InvalidTransition):
        assert_transition(current, new)


def test_assert_transition_accepts_string_values() -> None:
    assert_transition("queued", "running")  # type: ignore[arg-type]
    with pytest.raises(InvalidTransition):
        assert_transition("completed", "queued")  # type: ignore[arg-type]
