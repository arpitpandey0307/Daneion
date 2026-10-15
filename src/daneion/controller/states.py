"""Canonical states and legal transitions (ADR-0012, Scheduler spec §5.3).

A job's status is derived from its tasks once it is queued; tasks carry the attempt lifecycle.
"""

from __future__ import annotations

from enum import StrEnum


class JobState(StrEnum):
    SUBMITTED = "SUBMITTED"
    VALIDATING = "VALIDATING"
    ADMITTED = "ADMITTED"
    FITCHECK = "FITCHECK"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class TaskState(StrEnum):
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AttemptState(StrEnum):
    LEASED = "LEASED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class WorkerState(StrEnum):
    ONLINE = "ONLINE"
    SUSPECT = "SUSPECT"
    LOST = "LOST"
    PAUSED = "PAUSED"
    REVOKED = "REVOKED"


JOB_TERMINAL = {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED, JobState.REJECTED}
TASK_TERMINAL = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
TASK_ACTIVE = {TaskState.LEASED, TaskState.ACCEPTED, TaskState.RUNNING, TaskState.VERIFYING}
ATTEMPT_LIVE = {AttemptState.LEASED, AttemptState.ACCEPTED, AttemptState.RUNNING}

JOB_TRANSITIONS: dict[JobState, set[JobState]] = {
    JobState.SUBMITTED: {JobState.VALIDATING, JobState.CANCELLED},
    JobState.VALIDATING: {JobState.ADMITTED, JobState.REJECTED},
    JobState.ADMITTED: {JobState.FITCHECK, JobState.CANCELLED},
    JobState.FITCHECK: {JobState.QUEUED, JobState.REJECTED, JobState.CANCELLED},
    JobState.QUEUED: {JobState.RUNNING, JobState.CANCELLED, JobState.FAILED},
    JobState.RUNNING: {JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED},
}

TASK_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.QUEUED: {TaskState.LEASED, TaskState.CANCELLED},
    TaskState.LEASED: {TaskState.ACCEPTED, TaskState.RETRY_WAIT, TaskState.FAILED,
                       TaskState.CANCELLED},
    TaskState.ACCEPTED: {TaskState.RUNNING, TaskState.RETRY_WAIT, TaskState.FAILED,
                         TaskState.CANCELLED},
    TaskState.RUNNING: {TaskState.VERIFYING, TaskState.RETRY_WAIT, TaskState.FAILED,
                        TaskState.CANCELLED},
    TaskState.VERIFYING: {TaskState.SUCCEEDED, TaskState.RETRY_WAIT, TaskState.FAILED,
                          TaskState.CANCELLED},
    TaskState.RETRY_WAIT: {TaskState.LEASED, TaskState.CANCELLED, TaskState.FAILED},
}


class IllegalTransition(RuntimeError):
    pass


def check_job(src: str, dst: JobState) -> None:
    if dst not in JOB_TRANSITIONS.get(JobState(src), set()):
        raise IllegalTransition(f"job {src} -> {dst}")


def check_task(src: str, dst: TaskState) -> None:
    if dst not in TASK_TRANSITIONS.get(TaskState(src), set()):
        raise IllegalTransition(f"task {src} -> {dst}")
