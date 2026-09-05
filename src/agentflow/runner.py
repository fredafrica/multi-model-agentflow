"""Serial MVP runner with checkpoints, tests, and independent review."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .adapters import (
    AdapterRouter,
    InvocationIncompleteError,
    InvocationOutcomeUnknown,
    ModelAdapter,
    ReviewerProtocolError,
    ReviewerUnavailableError,
)
from .authorization import validate_authorization
from .contracts import (
    AuthorizationSnapshot,
    InvocationRequest,
    InvocationResult,
    ModelRef,
    PlanContract,
    ReviewFinding,
    ReviewResult,
    Severity,
    TaskContract,
)
from .database import Database
from .policies import changed_files_decision, review_independence_decision
from .service import (
    ConfirmationRequiredError,
    InvocationContext,
    InvocationService,
    PauseBlockedError,
    PolicyDeniedError,
)
from .states import TASK_TRANSITIONS, ControlState, InvocationState, RunState, TaskState
from .workspace import GitWorkspace


ConfirmationCallback = Callable[[InvocationRequest], bool]


def _valid_session_id(session_id: str | None) -> bool:
    if not session_id or not isinstance(session_id, str):
        return False
    return all(not (ch.isspace() or ord(ch) < 0x20) for ch in session_id)


@dataclass(frozen=True)
class RunResult:
    run_id: str
    state: RunState


class ReadOnlyReviewViolation(RuntimeError):
    pass


class MalformedCallMetadataError(RuntimeError):
    """A stored model call lacks the metadata needed to continue safely."""


REVIEWER_OUTPUT_PROTOCOL = """Reviewer output protocol (mandatory):
- Return exactly one JSON object and nothing else.
- Do not use a Markdown code fence, preface, summary, or explanatory prose.
- The top-level object must contain approved (boolean) and findings (array).
- Every finding must contain severity (P0, P1, P2, or P3), title (string), and explanation (string).
- A finding may also contain path (project-relative string) and remediation (string).
- If any P0 or P1 finding exists, approved must not be true.

Review packet (JSON data):
"""


class Runner:
    def __init__(
        self,
        database: Database,
        adapter: ModelAdapter,
        workspace: GitWorkspace,
        *,
        confirmation_callback: ConfirmationCallback | None = None,
    ) -> None:
        self.database = database
        self.adapter = adapter
        self.workspace = workspace
        self.confirmation_callback = confirmation_callback or (lambda _request: False)
        self.invocations = InvocationService(database, adapter)

    def start(
        self,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
        *,
        run_id: str | None = None,
    ) -> RunResult:
        validate_authorization(authorization, plan)
        self.workspace.require_committed_base()
        self.database.save_plan(plan)
        self.database.save_authorization(authorization)
        actual_run_id = run_id or str(uuid4())
        self.database.create_run(actual_run_id, plan, authorization)
        return self.execute(actual_run_id, plan, authorization)

    def resume(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
    ) -> RunResult:
        validate_authorization(authorization, plan)
        snapshot = self.database.run_snapshot(run_id)
        control = ControlState(snapshot["run"]["control_state"])
        if self.database.unresolved_unknown_calls(run_id):
            raise ValueError(
                "run has an UNKNOWN model call that must be reconciled before resume"
            )
        if self._has_blocking_incomplete_call(run_id, plan):
            raise ValueError(
                "run has a step-limit model call that cannot be continued; "
                "inspect it before resuming"
            )
        if control is ControlState.RUNNING:
            if self.database.inflight_calls(run_id):
                raise ValueError(
                    "run has an in-flight call; inspect it before recovering"
                )
            return self.execute(run_id, plan, authorization)
        if control not in (ControlState.PAUSED, ControlState.USER_TAKEOVER):
            raise ValueError("only paused or takeover runs can resume")
        self._restore_resolved_tasks(run_id, plan)
        if control is ControlState.USER_TAKEOVER:
            self._invalidate_takeover_changes(run_id, plan)
        self.database.transition_control(run_id, ControlState.RESUMING)
        self.database.transition_control(run_id, ControlState.RUNNING)
        self.database.set_run_state(run_id, RunState.RUNNING)
        return self.execute(run_id, plan, authorization)

    def execute(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
    ) -> RunResult:
        for task in plan.tasks:
            if self._pause_if_requested(run_id, task.task_id):
                return RunResult(run_id, RunState.PAUSED)
            state = self.database.task_state(run_id, task.task_id)
            if state is TaskState.APPROVED:
                continue
            try:
                self._execute_task(run_id, plan, authorization, task)
            except ConfirmationRequiredError:
                self._complete_safe_pause(run_id, task.task_id, "confirmation_required")
                return RunResult(run_id, RunState.PAUSED)
            except PolicyDeniedError:
                state = self.database.task_state(run_id, task.task_id)
                if state in (TaskState.RUNNING, TaskState.REVISING):
                    self.database.transition_task(
                        run_id, task.task_id, TaskState.AUTHORIZATION_REQUIRED
                    )
                elif state in (TaskState.WAITING_REVIEW, TaskState.WAITING_REREVIEW):
                    self.database.transition_task(
                        run_id, task.task_id, TaskState.WAITING_INPUT
                    )
                self._complete_safe_pause(run_id, task.task_id, "policy_denied")
                return RunResult(run_id, RunState.PAUSED)
            except InvocationOutcomeUnknown:
                if self.database.task_state(run_id, task.task_id) is TaskState.RUNNING:
                    self.database.transition_task(
                        run_id, task.task_id, TaskState.WAITING_INPUT
                    )
                self._complete_safe_pause(run_id, task.task_id, "unknown_model_call")
                return RunResult(run_id, RunState.PAUSED)
            except InvocationIncompleteError as error:
                state = self.database.task_state(run_id, task.task_id)
                if error.failure_kind == "session_mismatch":
                    reason = "session_mismatch"
                elif state in (TaskState.WAITING_REVIEW, TaskState.WAITING_REREVIEW):
                    reason = "review_step_limit_reached"
                else:
                    reason = "implementation_step_limit_reached"
                self._complete_safe_pause(run_id, task.task_id, reason)
                return RunResult(run_id, RunState.PAUSED)
            except ReviewerProtocolError:
                self._complete_safe_pause(
                    run_id, task.task_id, "reviewer_output_invalid"
                )
                return RunResult(run_id, RunState.PAUSED)
            except ReviewerUnavailableError:
                self._complete_safe_pause(run_id, task.task_id, "reviewer_unavailable")
                return RunResult(run_id, RunState.PAUSED)
            except ReadOnlyReviewViolation:
                self._move_to_failed(run_id, task.task_id)
                self.database.set_run_state(run_id, RunState.FAILED)
                return RunResult(run_id, RunState.FAILED)
            except (OSError, RuntimeError, ValueError):
                control = ControlState(
                    self.database.run_snapshot(run_id)["run"]["control_state"]
                )
                if control is ControlState.PAUSED:
                    self.database.save_checkpoint(
                        run_id, {"task_id": task.task_id, "reason": "immediate_freeze"}
                    )
                    return RunResult(run_id, RunState.PAUSED)
                state = self.database.task_state(run_id, task.task_id)
                if TaskState.FAILED in TASK_TRANSITIONS[state]:
                    self.database.transition_task(run_id, task.task_id, TaskState.FAILED)
                self.database.save_checkpoint(
                    run_id, {"task_id": task.task_id, "reason": "execution_error"}
                )
                self.database.set_run_state(run_id, RunState.FAILED)
                return RunResult(run_id, RunState.FAILED)
            snapshot = self.database.run_snapshot(run_id)
            if ControlState(snapshot["run"]["control_state"]) is ControlState.PAUSED:
                return RunResult(run_id, RunState.PAUSED)
            if self.database.task_state(run_id, task.task_id) is TaskState.FAILED:
                self.database.set_run_state(run_id, RunState.FAILED)
                return RunResult(run_id, RunState.FAILED)
        if self.database.unfinished_calls(run_id):
            self.database.set_run_state(run_id, RunState.FAILED)
            return RunResult(run_id, RunState.FAILED)
        self.database.set_run_state(run_id, RunState.COMPLETED)
        return RunResult(run_id, RunState.COMPLETED)

    def _execute_task(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
        task: TaskContract,
    ) -> None:
        worktree = self._ensure_worktree(run_id, task)

        while True:
            state = self.database.task_state(run_id, task.task_id)
            if state in (TaskState.APPROVED, TaskState.FAILED):
                return
            if state is TaskState.DRAFT:
                self.database.transition_task(
                    run_id, task.task_id, TaskState.WAITING_AUTHORIZATION
                )
                self.database.transition_task(run_id, task.task_id, TaskState.QUEUED)
                self.database.transition_task(run_id, task.task_id, TaskState.RUNNING)
                continue
            if state is TaskState.RUNNING:
                attempt_id, number = self._attempt_for_role(
                    run_id, task.task_id, "implementation"
                )
                if not self._drive_local_role(
                    run_id,
                    plan,
                    authorization,
                    task,
                    attempt_id,
                    number,
                    worktree,
                    role="implementation",
                ):
                    return
                if not self._files_are_allowed(task, worktree):
                    self.database.complete_attempt(attempt_id, "file_scope_failed")
                    self._move_to_failed(run_id, task.task_id)
                    return
                self.database.transition_task(run_id, task.task_id, TaskState.SELF_TESTING)
                if self._pause_if_requested(run_id, task.task_id):
                    return
                continue
            if state is TaskState.SELF_TESTING:
                attempt = self._require_attempt(run_id, task.task_id)
                if not self._files_are_allowed(task, worktree):
                    self.database.complete_attempt(attempt["attempt_id"], "file_scope_failed")
                    self._move_to_failed(run_id, task.task_id)
                    return
                if self._run_tests(run_id, task, attempt["attempt_id"], worktree):
                    self.database.transition_task(
                        run_id, task.task_id, TaskState.WAITING_REVIEW
                    )
                elif int(attempt["attempt_number"]) <= task.max_retry_count:
                    self.database.transition_task(run_id, task.task_id, TaskState.REVISING)
                else:
                    self.database.complete_attempt(attempt["attempt_id"], "tests_failed")
                    self._move_to_failed(run_id, task.task_id)
                    return
                if self._pause_if_requested(run_id, task.task_id):
                    return
                continue
            if state is TaskState.WAITING_REVIEW:
                attempt = self._require_attempt(run_id, task.task_id)
                approved = self._review(
                    run_id, plan, authorization, task, attempt["attempt_id"], worktree, False
                )
                if approved:
                    self.database.complete_attempt(attempt["attempt_id"], "approved")
                    self.database.set_task_baseline(
                        run_id, task.task_id, self.workspace.status_snapshot(worktree)
                    )
                    self.database.transition_task(run_id, task.task_id, TaskState.APPROVED)
                elif int(attempt["attempt_number"]) <= task.max_retry_count:
                    self.database.transition_task(run_id, task.task_id, TaskState.REVISING)
                else:
                    self.database.complete_attempt(attempt["attempt_id"], "review_failed")
                    self._move_to_failed(run_id, task.task_id)
                if self._pause_if_requested(run_id, task.task_id):
                    return
                continue
            if state is TaskState.REVISING:
                attempt_id, number = self._attempt_for_role(
                    run_id, task.task_id, "revision"
                )
                while True:
                    if not self._drive_local_role(
                        run_id,
                        plan,
                        authorization,
                        task,
                        attempt_id,
                        number,
                        worktree,
                        role="revision",
                        prompt=self._revision_prompt(run_id, task, worktree),
                    ):
                        return
                    files_ok = self._files_are_allowed(task, worktree)
                    tests_ok = files_ok and self._run_tests(
                        run_id, task, attempt_id, worktree
                    )
                    if tests_ok:
                        self.database.transition_task(
                            run_id, task.task_id, TaskState.WAITING_REREVIEW
                        )
                        break
                    self.database.complete_attempt(attempt_id, "revision_failed")
                    if number - 1 >= task.max_retry_count:
                        self._move_to_failed(run_id, task.task_id)
                        return
                    attempt_id, number = self._new_attempt(run_id, task.task_id)
                if self._pause_if_requested(run_id, task.task_id):
                    return
                continue
            if state is TaskState.WAITING_REREVIEW:
                attempt = self._require_attempt(run_id, task.task_id)
                approved = self._review(
                    run_id, plan, authorization, task, attempt["attempt_id"], worktree, True
                )
                if approved:
                    self.database.complete_attempt(attempt["attempt_id"], "approved")
                    self.database.set_task_baseline(
                        run_id, task.task_id, self.workspace.status_snapshot(worktree)
                    )
                    self.database.transition_task(run_id, task.task_id, TaskState.APPROVED)
                else:
                    self.database.complete_attempt(attempt["attempt_id"], "rereview_failed")
                    self._move_to_failed(run_id, task.task_id)
                if self._pause_if_requested(run_id, task.task_id):
                    return
                continue
            raise RuntimeError(f"cannot execute task from state {state.value}")

    def _ensure_worktree(self, run_id: str, task: TaskContract) -> Path:
        snapshot = self.database.run_snapshot(run_id)
        row = next(item for item in snapshot["tasks"] if item["task_id"] == task.task_id)
        if row["worktree_path"]:
            return Path(row["worktree_path"])
        path = self.workspace.create(run_id, task.task_id)
        self.database.set_task_worktree(run_id, task.task_id, str(path))
        return path

    def _new_attempt(self, run_id: str, task_id: str) -> tuple[str, int]:
        latest = self.database.latest_attempt(run_id, task_id)
        number = int(latest["attempt_number"]) + 1 if latest else 1
        attempt_id = str(uuid4())
        self.database.create_attempt(attempt_id, run_id, task_id, number)
        return attempt_id, number

    def _attempt_for_role(
        self, run_id: str, task_id: str, role: str
    ) -> tuple[str, int]:
        latest = self.database.latest_attempt(run_id, task_id)
        if latest is not None and latest["outcome"] is None:
            call = self.database.fetch_one(
                """
                SELECT role, state FROM model_calls
                WHERE attempt_id = ? ORDER BY started_at DESC LIMIT 1
                """,
                (latest["attempt_id"],),
            )
            if call is not None and call["role"] == role:
                return str(latest["attempt_id"]), int(latest["attempt_number"])
        return self._new_attempt(run_id, task_id)

    def _require_attempt(self, run_id: str, task_id: str):
        attempt = self.database.latest_attempt(run_id, task_id)
        if attempt is None:
            raise RuntimeError(f"task {task_id} has no attempt")
        return attempt

    def _invoke_role(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
        task: TaskContract,
        attempt_id: str,
        attempt_number: int,
        role: str,
        model: ModelRef,
        worktree: Path,
        *,
        read_only: bool,
        prompt: str | None = None,
        audit_metadata: dict[str, object] | None = None,
        reuse_planned: bool = False,
        planned_call_id: str | None = None,
        planned_request_key: str | None = None,
    ) -> tuple[InvocationResult, ModelRef]:
        candidates: list[ModelRef] = []
        recovery_candidates = (model,) if reuse_planned else (model, task.fallback_model)
        for candidate in recovery_candidates:
            if candidate is None or candidate in candidates:
                continue
            if read_only and not review_independence_decision(
                replace(task, review_model=candidate)
            ).allowed:
                continue
            candidates.append(candidate)
        if not candidates:
            raise ReviewerUnavailableError("reviewer independence denied")
        first_denial: PolicyDeniedError | ReviewerUnavailableError | None = None
        for candidate in candidates:
            call_id = planned_call_id or str(uuid4())
            selected_adapter = (
                self.adapter.adapter_for(candidate.provider)
                if isinstance(self.adapter, AdapterRouter)
                else self.adapter
            )
            request = InvocationRequest(
                call_id=call_id,
                request_key=planned_request_key
                or (
                    f"{run_id}:{task.task_id}:{attempt_number}:{role}:"
                    f"{candidate.registry_key}"
                ),
                run_id=run_id,
                task_id=task.task_id,
                role=role,
                model=candidate,
                prompt=prompt or self._implementation_prompt(task),
                data_sensitivity=task.data_sensitivity,
                read_only=read_only,
                metadata={
                    "worktree": str(worktree),
                    "allowed_files": task.allowed_files,
                    "implementation_max_steps": task.implementation_max_steps,
                    "implementation_timeout_seconds": task.implementation_timeout_seconds,
                    "estimated_remote_cost": (
                        0 if candidate.is_local else task.max_remote_cost
                    ),
                    "test_double": bool(
                        getattr(selected_adapter, "test_double", False)
                    ),
                    "on_provider_request_id": (
                        lambda provider_request_id, call_id=call_id: (
                            self.database.set_provider_request_id(
                                call_id, provider_request_id
                            )
                        )
                    ),
                    **(audit_metadata or {}),
                },
            )
            context = InvocationContext(
                plan,
                task,
                authorization,
                attempt_id,
                estimated_remote_cost=0
                if candidate.is_local
                else task.max_remote_cost,
            )
            try:
                return self._invoke_with_confirmation(
                    request, context, reuse_planned=reuse_planned
                ), candidate
            except (PolicyDeniedError, ReviewerUnavailableError) as error:
                first_denial = first_denial or error
                if candidate == candidates[-1]:
                    raise first_denial
                self.database.record_model_fallback(
                    run_id,
                    task.task_id,
                    role,
                    candidate.registry_key,
                    candidates[-1].registry_key,
                )
        if first_denial is not None:
            raise first_denial
        raise PolicyDeniedError("no independent authorized model is available for this role")

    def _invoke_with_confirmation(
        self,
        request: InvocationRequest,
        context: InvocationContext,
        *,
        reuse_planned: bool = False,
    ) -> InvocationResult:
        try:
            return self.invocations.invoke(
                request, context, confirmed=False, reuse_planned=reuse_planned
            )
        except ConfirmationRequiredError:
            if not self.confirmation_callback(request):
                raise
            return self.invocations.invoke(
                request, context, confirmed=True, reuse_planned=reuse_planned
            )

    def _drive_local_role(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
        task: TaskContract,
        attempt_id: str,
        number: int,
        worktree: Path,
        *,
        role: str,
        prompt: str | None = None,
    ) -> bool:
        """Invoke a local write role, resuming the same session across segments.

        Returns ``True`` when the role call completed and the worktree stayed
        inside the allowed file scope, or ``False`` when a file-scope violation
        already failed the task. Step-limit terminations are continued in a new
        segment of the same OpenCode session up to ``implementation_max_continuations``.
        """
        model = task.implementation_model
        last = self.database.latest_role_call(attempt_id, role)
        if last is not None and last["state"] == InvocationState.COMPLETED.value:
            return True
        base_planned = (
            last is not None
            and last["state"] == InvocationState.PLANNED.value
            and self._classify_planned_call(attempt_id, role, last) == "base"
        )
        if last is None or base_planned:
            try:
                if base_planned:
                    model = self._model_from_call(last)
                self._invoke_role(
                    run_id,
                    plan,
                    authorization,
                    task,
                    attempt_id,
                    number,
                    role,
                    model,
                    worktree,
                    read_only=False,
                    prompt=prompt,
                    reuse_planned=base_planned,
                    planned_call_id=str(last["call_id"]) if base_planned else None,
                    planned_request_key=(
                        str(last["request_key"]) if base_planned else None
                    ),
                )
            except MalformedCallMetadataError:
                self._complete_safe_pause(
                    run_id, task.task_id, "malformed_base_call_metadata"
                )
                return False
            except PauseBlockedError:
                self._complete_safe_pause(run_id, task.task_id, "pause_requested")
                return False
            except InvocationIncompleteError as error:
                if error.failure_kind != "step_limit_reached":
                    raise
            else:
                return True
        while True:
            if self._continuation_pause_requested(run_id, task.task_id):
                return False
            last = self.database.latest_role_call(attempt_id, role)
            if last is None or last["state"] == InvocationState.COMPLETED.value:
                return True
            state = InvocationState(last["state"])
            try:
                if state is InvocationState.PLANNED:
                    if self._classify_planned_call(attempt_id, role, last) != "continuation":
                        raise MalformedCallMetadataError(
                            "stored PLANNED call has malformed continuation metadata"
                        )
                    if not self._files_are_allowed(task, worktree):
                        self.database.complete_attempt(attempt_id, "file_scope_failed")
                        self._move_to_failed(run_id, task.task_id)
                        return False
                    model = self._model_from_call(last)
                    segment_index = int(last["segment_index"])
                    continuation_of_call_id = str(last["continuation_of_call_id"])
                    session_id = last["continuation_session_id"]
                elif state is InvocationState.FAILED:
                    metadata = (
                        json.loads(last["raw_metadata_json"])
                        if last["raw_metadata_json"]
                        else {}
                    )
                    if metadata.get("failure_kind") != "step_limit_reached":
                        raise InvocationIncompleteError(
                            "local invocation did not complete and cannot be continued",
                            self._incomplete_result(last),
                            failure_kind=metadata.get("failure_kind")
                            or "step_limit_reached",
                        )
                    if not self._continuation_allowed(task, last):
                        raise InvocationIncompleteError(
                            "implementation step limit reached without an authorized continuation",
                            self._incomplete_result(last),
                            failure_kind="step_limit_reached",
                        )
                    if not self._files_are_allowed(task, worktree):
                        self.database.complete_attempt(attempt_id, "file_scope_failed")
                        self._move_to_failed(run_id, task.task_id)
                        return False
                    model = self._model_from_call(last)
                    segment_index = int(last["segment_index"]) + 1
                    continuation_of_call_id = str(last["call_id"])
                    session_id = last["provider_request_id"]
                else:
                    raise InvocationIncompleteError(
                        "local invocation did not complete and cannot be continued",
                        self._incomplete_result(last),
                        failure_kind="step_limit_reached",
                    )
            except MalformedCallMetadataError:
                self._complete_safe_pause(
                    run_id, task.task_id, "malformed_continuation_metadata"
                )
                return False
            try:
                self._invoke_continuation(
                    run_id,
                    plan,
                    authorization,
                    task,
                    attempt_id,
                    number,
                    role,
                    model,
                    worktree,
                    segment_index=segment_index,
                    continuation_of_call_id=continuation_of_call_id,
                    session_id=session_id,
                )
            except PauseBlockedError:
                self._complete_safe_pause(run_id, task.task_id, "pause_requested")
                return False
            except InvocationIncompleteError as error:
                if error.failure_kind != "step_limit_reached":
                    raise
                continue
            return True

    def _invoke_continuation(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
        task: TaskContract,
        attempt_id: str,
        number: int,
        role: str,
        model: ModelRef,
        worktree: Path,
        *,
        segment_index: int,
        continuation_of_call_id: str,
        session_id: str | None,
    ) -> InvocationResult:
        request_key = (
            f"{run_id}:{task.task_id}:{number}:{role}:{model.registry_key}"
            f":segment:{segment_index}"
        )
        existing = self.database.fetch_one(
            "SELECT call_id, state FROM model_calls WHERE request_key = ?",
            (request_key,),
        )
        if (
            existing is not None
            and InvocationState(existing["state"]) is InvocationState.PLANNED
        ):
            call_id = str(existing["call_id"])
        else:
            call_id = str(uuid4())
        selected_adapter = (
            self.adapter.adapter_for(model.provider)
            if isinstance(self.adapter, AdapterRouter)
            else self.adapter
        )
        request = InvocationRequest(
            call_id=call_id,
            request_key=request_key,
            run_id=run_id,
            task_id=task.task_id,
            role=role,
            model=model,
            prompt=self._continuation_prompt(task),
            data_sensitivity=task.data_sensitivity,
            read_only=False,
            metadata={
                "worktree": str(worktree),
                "allowed_files": task.allowed_files,
                "implementation_max_steps": task.implementation_max_steps,
                "implementation_timeout_seconds": task.implementation_timeout_seconds,
                "estimated_remote_cost": 0 if model.is_local else task.max_remote_cost,
                "test_double": bool(getattr(selected_adapter, "test_double", False)),
                "segment_index": segment_index,
                "continuation_of_call_id": continuation_of_call_id,
                "continuation_session_id": session_id,
                "on_provider_request_id": (
                    lambda provider_request_id, call_id=call_id: (
                        self.database.set_provider_request_id(call_id, provider_request_id)
                    )
                ),
            },
        )
        context = InvocationContext(
            plan,
            task,
            authorization,
            attempt_id,
            estimated_remote_cost=0 if model.is_local else task.max_remote_cost,
        )
        return self._invoke_with_confirmation(request, context, reuse_planned=True)

    @staticmethod
    def _continuation_allowed(task: TaskContract, last) -> bool:
        if last["role"] not in ("implementation", "revision"):
            return False
        if not last["is_local"]:
            return False
        if not _valid_session_id(last["provider_request_id"]):
            return False
        return int(last["segment_index"]) + 1 <= task.implementation_max_continuations

    @staticmethod
    def _model_from_call(call) -> ModelRef:
        provider = call["provider"]
        model_id = call["model_id"]
        version = call["model_version"]
        family = call["model_family"]
        if not provider or not model_id or not version or not family:
            raise MalformedCallMetadataError(
                "stored model call is missing provider/model/version/family metadata"
            )
        return ModelRef(
            provider=str(provider),
            model_id=str(model_id),
            version=str(version),
            family=str(family),
            is_local=bool(call["is_local"]),
        )

    def _classify_planned_call(self, attempt_id: str, role: str, call) -> str:
        """Classify a PLANNED call as ``base``, ``continuation``, or ``malformed``.

        A call is base only when ``segment_index == 0`` and both
        ``continuation_of_call_id`` and ``continuation_session_id`` are NULL. A
        call is a continuation only when ``segment_index >= 1``, its
        ``continuation_of_call_id`` names a real parent call in the same
        attempt/role chain, and its ``continuation_session_id`` is a valid
        session ID. Every other combination is malformed.
        """
        segment_index = int(call["segment_index"] or 0)
        parent = call["continuation_of_call_id"]
        session_id = call["continuation_session_id"]
        if segment_index == 0:
            if parent is None and session_id is None:
                return "base"
            return "malformed"
        if not isinstance(parent, str) or parent == "None" or not _valid_session_id(parent):
            return "malformed"
        if not _valid_session_id(session_id):
            return "malformed"
        parent_row = self.database.fetch_one(
            "SELECT attempt_id, role, segment_index, provider_request_id, "
            "provider, model_id, model_version, model_family, is_local "
            "FROM model_calls WHERE call_id = ?",
            (parent,),
        )
        if parent_row is None:
            return "malformed"
        if parent_row["attempt_id"] != attempt_id or parent_row["role"] != role:
            return "malformed"
        if int(parent_row["segment_index"] or 0) != segment_index - 1:
            return "malformed"
        if parent_row["provider_request_id"] != session_id:
            return "malformed"
        for field in (
            "provider",
            "model_id",
            "model_version",
            "model_family",
            "is_local",
        ):
            if parent_row[field] != call[field]:
                return "malformed"
        return "continuation"

    @staticmethod
    def _incomplete_result(last) -> InvocationResult:
        return InvocationResult(
            provider_request_id=last["provider_request_id"],
            output="",
            input_tokens=0,
            output_tokens=0,
            first_token_latency_ms=None,
            duration_ms=0,
            remote_cost=0.0,
            raw_metadata={"failure_kind": "step_limit_reached"},
            cost_unavailable=False,
        )

    @staticmethod
    def _continuation_prompt(task: TaskContract) -> str:
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
        forbidden = "\n".join(f"- {item}" for item in task.forbidden_actions)
        return (
            "You are continuing a previous implementation segment in the same isolated "
            "Git worktree and OpenCode session. Do not re-investigate the whole repository.\n"
            "1. Inspect the current diff and any still-missing expected outputs first.\n"
            "2. Prioritize completing any expected output that is still missing.\n"
            "3. For large files, create a minimal importable skeleton first, then grow it "
            "with several small edit/apply_patch calls, confirming the file exists after each.\n"
            "4. Never submit a single oversized write and never repeat a write call that is "
            "missing its content argument; if a write tool reports a schema error, shrink the "
            "patch and retry.\n"
            "5. Do not bypass file permissions via shell heredoc, cat redirection, or external "
            "scripts.\n"
            "6. Complete the implementation and self-check; do not only summarize remaining work.\n\n"
            f"Objective: {task.objective}\n"
            f"Acceptance criteria:\n{criteria}\n"
            f"Forbidden actions:\n{forbidden}"
        )

    def _revision_prompt(self, run_id: str, task: TaskContract, worktree: Path) -> str:
        test = self.database.latest_test(run_id, task.task_id)
        evidence: dict[str, object] = {}
        if test is not None:
            evidence = json.loads(test["evidence_json"])
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
        forbidden = "\n".join(f"- {item}" for item in task.forbidden_actions)
        missing = evidence.get("missing_outputs", ()) or ()
        changed = self.workspace.changed_files(worktree)
        parts = [
            "The deterministic tests for this task failed. Fix the implementation and "
            "complete the missing expected outputs before the tests are re-run.",
            f"Objective: {task.objective}",
            f"Acceptance criteria:\n{criteria}",
            f"Forbidden actions:\n{forbidden}",
            f"Test return code: {evidence.get('returncode')}",
            f"Missing expected outputs: {', '.join(str(item) for item in missing) or '(none)'}",
            f"Changed files: {', '.join(changed) or '(none)'}",
        ]
        stderr = str(evidence.get("stderr", ""))[-4000:]
        if stderr:
            parts.append(f"Test stderr (tail):\n{stderr}")
        stdout = str(evidence.get("stdout", ""))[-4000:]
        if stdout:
            parts.append(f"Test stdout (tail):\n{stdout}")
        return "\n\n".join(parts)

    def _has_blocking_incomplete_call(self, run_id: str, plan: PlanContract) -> bool:
        tasks = {task.task_id: task for task in plan.tasks}
        for call in self.database.step_limit_calls_without_continuation(run_id):
            task = tasks.get(call["task_id"])
            if task is None or not self._continuation_allowed(task, call):
                return True
        return False

    @staticmethod
    def _implementation_prompt(task: TaskContract) -> str:
        criteria = "\n".join(f"- {item}" for item in task.acceptance_criteria)
        forbidden = "\n".join(f"- {item}" for item in task.forbidden_actions)
        return (
            f"Objective: {task.objective}\n"
            f"Acceptance criteria:\n{criteria}\n"
            f"Forbidden actions:\n{forbidden}"
        )

    def _run_tests(
        self, run_id: str, task: TaskContract, attempt_id: str, worktree: Path
    ) -> bool:
        result = self.workspace.run_test(worktree, task.test_command)
        missing_outputs = tuple(
            path for path in task.expected_outputs if not (worktree / path).is_file()
        )
        untracked_whitespace_errors = self.workspace.untracked_whitespace_errors(
            worktree
        )
        passed = (
            result.passed
            and not missing_outputs
            and not untracked_whitespace_errors
        )
        self.database.record_test(
            str(uuid4()),
            run_id,
            task.task_id,
            attempt_id,
            source="deterministic",
            passed=passed,
            duration_ms=result.duration_ms,
            evidence={
                "command": task.test_command,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "duration_ms": result.duration_ms,
                "missing_outputs": missing_outputs,
                "untracked_whitespace_errors": untracked_whitespace_errors,
            },
        )
        return passed

    def _files_are_allowed(self, task: TaskContract, worktree: Path) -> bool:
        decision = changed_files_decision(task.allowed_files, self.workspace.changed_files(worktree))
        return decision.allowed

    def _review(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
        task: TaskContract,
        attempt_id: str,
        worktree: Path,
        rereview: bool,
    ) -> bool:
        before = self.workspace.status_snapshot(worktree)
        diff = self.workspace.diff(worktree)
        test = self.database.latest_test(run_id, task.task_id)
        packet = {
            "task_id": task.task_id,
            "objective": task.objective,
            "risk_level": {
                "business_importance": task.risk_level.business_importance.value,
                "operational_safety": task.risk_level.operational_safety.value,
            },
            "acceptance_criteria": task.acceptance_criteria,
            "forbidden_actions": task.forbidden_actions,
            "diff": diff,
            "deterministic_test": self._review_test_evidence(test),
            "evidence_excerpts": (),
            "review_questions": (
                "Does the actual diff satisfy every acceptance criterion?",
                "Do tests and evidence reveal any P0-P3 finding?",
            ),
            "expected_response": {
                "approved": "boolean",
                "findings": [
                    {
                        "severity": "P0|P1|P2|P3",
                        "title": "text",
                        "explanation": "text",
                        "path": "optional project-relative path",
                        "remediation": "optional text",
                    }
                ],
            },
        }
        packet_json = json.dumps(
            packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt = REVIEWER_OUTPUT_PROTOCOL + packet_json
        packet_bytes = packet_json.encode("utf-8")
        attempt_number = int(self._require_attempt(run_id, task.task_id)["attempt_number"])
        result, reviewer = self._invoke_role(
            run_id,
            plan,
            authorization,
            task,
            attempt_id,
            attempt_number,
            "rereview" if rereview else "review",
            task.review_model,
            worktree,
            read_only=True,
            prompt=prompt,
            audit_metadata={
                "packet_hash": hashlib.sha256(packet_bytes).hexdigest(),
                "packet_size": len(packet_bytes),
                "privacy_policy_version": plan.privacy_policy_version,
            },
        )
        if self.workspace.status_snapshot(worktree) != before:
            raise ReadOnlyReviewViolation("reviewer modified the worktree")
        try:
            review = self._parse_review(task, reviewer, result.output)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise ReviewerProtocolError(
                "reviewer output did not satisfy the required JSON protocol"
            ) from error
        self.database.record_review(
            review.review_id,
            run_id,
            task.task_id,
            attempt_id,
            provider=review.reviewer.provider,
            model_id=review.reviewer.model_id,
            model_version=review.reviewer.version,
            approved=review.approved,
            findings=review.findings,
        )
        return review.approved

    @staticmethod
    def _parse_review(task: TaskContract, reviewer: ModelRef, text: str) -> ReviewResult:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("review output must be a JSON object")
        if "approved" not in data or not isinstance(data["approved"], bool):
            raise ValueError("review approved must be a boolean")
        if "findings" not in data or not isinstance(data["findings"], list):
            raise ValueError("review findings must be an array")
        findings_list: list[ReviewFinding] = []
        for item in data["findings"]:
            if not isinstance(item, dict):
                raise ValueError("every review finding must be an object")
            for field in ("severity", "title", "explanation"):
                if field not in item:
                    raise ValueError(f"review finding is missing {field}")
            if not isinstance(item["severity"], str):
                raise ValueError("review finding severity must be a string")
            severity = Severity(item["severity"])
            if not isinstance(item["title"], str) or not item["title"]:
                raise ValueError("review finding title must be a non-empty string")
            if not isinstance(item["explanation"], str) or not item["explanation"]:
                raise ValueError(
                    "review finding explanation must be a non-empty string"
                )
            for field in ("path", "remediation"):
                if field in item and (
                    not isinstance(item[field], str) or not item[field]
                ):
                    raise ValueError(
                        f"review finding {field} must be a non-empty string"
                    )
            findings_list.append(
                ReviewFinding(
                    severity=severity,
                    title=item["title"],
                    explanation=item["explanation"],
                    blocking=severity in (Severity.P0, Severity.P1),
                    path=item.get("path"),
                    remediation=item.get("remediation"),
                )
            )
        findings = tuple(findings_list)
        return ReviewResult(
            review_id=str(uuid4()),
            task_id=task.task_id,
            reviewer=reviewer,
            findings=findings,
            approved=data["approved"]
            and not any(
                finding.severity in (Severity.P0, Severity.P1)
                for finding in findings
            ),
        )

    @staticmethod
    def _review_test_evidence(test) -> dict[str, object] | None:
        if test is None:
            return None
        evidence = json.loads(test["evidence_json"])
        return {
            "passed": bool(test["passed"]),
            "duration_ms": test["duration_ms"],
            "command": evidence.get("command", ()),
            "returncode": evidence.get("returncode"),
            "stdout_excerpt": str(evidence.get("stdout", ""))[-20_000:],
            "stderr_excerpt": str(evidence.get("stderr", ""))[-20_000:],
            "missing_outputs": evidence.get("missing_outputs", ()),
            "untracked_whitespace_errors": evidence.get(
                "untracked_whitespace_errors", ()
            ),
        }

    def _continuation_pause_requested(self, run_id: str, task_id: str) -> bool:
        control = ControlState(self.database.run_snapshot(run_id)["run"]["control_state"])
        if control is ControlState.RUNNING:
            return False
        self._complete_safe_pause(run_id, task_id, "pause_requested")
        return True

    def _pause_if_requested(self, run_id: str, task_id: str) -> bool:
        snapshot = self.database.run_snapshot(run_id)
        control = ControlState(snapshot["run"]["control_state"])
        if control is ControlState.PAUSED:
            self.database.set_run_state(run_id, RunState.PAUSED)
            return True
        if control is not ControlState.PAUSE_REQUESTED:
            return False
        self._complete_safe_pause(run_id, task_id, "pause_requested")
        return True

    def _complete_safe_pause(self, run_id: str, task_id: str, reason: str) -> None:
        control = ControlState(self.database.run_snapshot(run_id)["run"]["control_state"])
        if control is ControlState.RUNNING:
            self.database.transition_control(run_id, ControlState.PAUSE_REQUESTED)
        control = ControlState(self.database.run_snapshot(run_id)["run"]["control_state"])
        if control is ControlState.PAUSE_REQUESTED:
            self.database.transition_control(run_id, ControlState.QUIESCING)
        self.database.save_checkpoint(
            run_id,
            {"task_id": task_id, "boundary": "before_next_call", "reason": reason},
        )
        control = ControlState(self.database.run_snapshot(run_id)["run"]["control_state"])
        if control is ControlState.QUIESCING:
            self.database.transition_control(run_id, ControlState.PAUSED)
        self.database.set_run_state(run_id, RunState.PAUSED)

    def _move_to_failed(self, run_id: str, task_id: str) -> None:
        state = self.database.task_state(run_id, task_id)
        if TaskState.FAILED in TASK_TRANSITIONS[state]:
            self.database.transition_task(run_id, task_id, TaskState.FAILED)
            return
        raise RuntimeError(f"cannot mark task failed from {state.value}")

    def _restore_resolved_tasks(self, run_id: str, plan: PlanContract) -> None:
        for task in plan.tasks:
            if self.database.task_state(run_id, task.task_id) is not TaskState.WAITING_INPUT:
                continue
            attempt = self.database.latest_attempt(run_id, task.task_id)
            if attempt is None:
                continue
            call = self.database.fetch_one(
                """
                SELECT role, state FROM model_calls
                WHERE attempt_id = ? ORDER BY started_at DESC LIMIT 1
                """,
                (attempt["attempt_id"],),
            )
            if (
                call is not None
                and call["role"] == "implementation"
                and call["state"] == "completed"
            ):
                self.database.transition_task(run_id, task.task_id, TaskState.RUNNING)

    def _invalidate_takeover_changes(self, run_id: str, plan: PlanContract) -> None:
        snapshot = self.database.run_snapshot(run_id)
        rows = {row["task_id"]: row for row in snapshot["tasks"]}
        affected: set[str] = set()
        for task in plan.tasks:
            row = self.database.fetch_one(
                """
                SELECT state, worktree_path, file_baseline_json FROM tasks
                WHERE run_id = ? AND task_id = ?
                """,
                (run_id, task.task_id),
            )
            if row is None or row["state"] != TaskState.APPROVED.value:
                continue
            if not row["worktree_path"] or not row["file_baseline_json"]:
                continue
            baseline = json.loads(row["file_baseline_json"])["snapshot"]
            if self.workspace.status_snapshot(row["worktree_path"]) != baseline:
                affected.add(task.task_id)

        changed = True
        while changed:
            changed = False
            for task in plan.tasks:
                if task.task_id not in affected and set(task.depends_on) & affected:
                    affected.add(task.task_id)
                    changed = True

        contracts = {task.task_id: task for task in plan.tasks}
        for task_id in affected:
            task = contracts[task_id]
            row = rows[task_id]
            files_ok = self._files_are_allowed(task, Path(row["worktree_path"]))
            target = TaskState.SELF_TESTING if files_ok else TaskState.FAILED
            self.database.invalidate_approved_task(
                run_id,
                task_id,
                target,
                "user changes detected" if files_ok else "user changes exceeded file scope",
            )
            if files_ok:
                self._new_attempt(run_id, task_id)
