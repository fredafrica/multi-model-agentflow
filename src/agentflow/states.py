"""Orthogonal task, control, and invocation state machines."""

from __future__ import annotations

from enum import Enum


class StringEnum(str, Enum):
    pass


class TaskState(StringEnum):
    DRAFT = "draft"
    WAITING_AUTHORIZATION = "waiting_authorization"
    QUEUED = "queued"
    RUNNING = "running"
    SELF_TESTING = "self_testing"
    WAITING_REVIEW = "waiting_review"
    REVISING = "revising"
    WAITING_REREVIEW = "waiting_rereview"
    APPROVED = "approved"
    WAITING_INPUT = "waiting_input"
    AUTHORIZATION_REQUIRED = "authorization_required"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class ControlState(StringEnum):
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    QUIESCING = "QUIESCING"
    PAUSED = "PAUSED"
    USER_TAKEOVER = "USER_TAKEOVER"
    RESUMING = "RESUMING"


class RunState(StringEnum):
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class InvocationState(StringEnum):
    PLANNED = "planned"
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.DRAFT: frozenset(
        {
            TaskState.WAITING_AUTHORIZATION,
            TaskState.WAITING_INPUT,
            TaskState.FAILED,
            TaskState.CANCELLED,
        }
    ),
    TaskState.WAITING_AUTHORIZATION: frozenset(
        {TaskState.QUEUED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.QUEUED: frozenset(
        {TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.SELF_TESTING,
            TaskState.WAITING_INPUT,
            TaskState.AUTHORIZATION_REQUIRED,
            TaskState.FAILED,
            TaskState.TIMED_OUT,
        }
    ),
    TaskState.SELF_TESTING: frozenset(
        {TaskState.WAITING_REVIEW, TaskState.REVISING, TaskState.FAILED}
    ),
    TaskState.WAITING_REVIEW: frozenset(
        {
            TaskState.APPROVED,
            TaskState.REVISING,
            TaskState.WAITING_INPUT,
            TaskState.FAILED,
        }
    ),
    TaskState.REVISING: frozenset(
        {TaskState.WAITING_REREVIEW, TaskState.AUTHORIZATION_REQUIRED, TaskState.FAILED}
    ),
    TaskState.WAITING_REREVIEW: frozenset(
        {TaskState.APPROVED, TaskState.REVISING, TaskState.WAITING_INPUT, TaskState.FAILED}
    ),
    TaskState.WAITING_INPUT: frozenset(
        {TaskState.RUNNING, TaskState.QUEUED, TaskState.REVISING, TaskState.CANCELLED}
    ),
    TaskState.AUTHORIZATION_REQUIRED: frozenset(
        {TaskState.QUEUED, TaskState.REVISING, TaskState.CANCELLED}
    ),
    TaskState.DEGRADED: frozenset(
        {TaskState.RUNNING, TaskState.WAITING_REVIEW, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.APPROVED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
    TaskState.TIMED_OUT: frozenset(),
}


CONTROL_TRANSITIONS: dict[ControlState, frozenset[ControlState]] = {
    ControlState.RUNNING: frozenset({ControlState.PAUSE_REQUESTED}),
    ControlState.PAUSE_REQUESTED: frozenset({ControlState.QUIESCING}),
    ControlState.QUIESCING: frozenset({ControlState.PAUSED}),
    ControlState.PAUSED: frozenset({ControlState.USER_TAKEOVER, ControlState.RESUMING}),
    ControlState.USER_TAKEOVER: frozenset({ControlState.RESUMING}),
    ControlState.RESUMING: frozenset({ControlState.RUNNING, ControlState.PAUSED}),
}


INVOCATION_TRANSITIONS: dict[InvocationState, frozenset[InvocationState]] = {
    InvocationState.PLANNED: frozenset({InvocationState.STARTED, InvocationState.CANCELLED}),
    InvocationState.STARTED: frozenset(
        {InvocationState.COMPLETED, InvocationState.FAILED, InvocationState.UNKNOWN}
    ),
    InvocationState.UNKNOWN: frozenset(
        {InvocationState.COMPLETED, InvocationState.FAILED}
    ),
    InvocationState.COMPLETED: frozenset(),
    InvocationState.FAILED: frozenset(),
    InvocationState.CANCELLED: frozenset(),
}


def require_transition(current: StringEnum, target: StringEnum) -> None:
    tables = {
        TaskState: TASK_TRANSITIONS,
        ControlState: CONTROL_TRANSITIONS,
        InvocationState: INVOCATION_TRANSITIONS,
    }
    if type(current) is not type(target):
        raise ValueError("cannot transition between different state machines")
    allowed = tables[type(current)][current]
    if target not in allowed:
        raise ValueError(f"invalid transition: {current.value} -> {target.value}")
