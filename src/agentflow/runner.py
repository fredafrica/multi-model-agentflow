"""Serial MVP runner with checkpoints, tests, and independent review."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from .adapters import InvocationOutcomeUnknown, ModelAdapter
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
    PolicyDeniedError,
)
from .states import TASK_TRANSITIONS, ControlState, RunState, TaskState
from .workspace import GitWorkspace


ConfirmationCallback = Callable[[InvocationRequest], bool]


@dataclass(frozen=True)
class RunResult:
    run_id: str
    state: RunState


class ReadOnlyReviewViolation(RuntimeError):
    pass


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
        if control is ControlState.RUNNING:
            if self.database.inflight_calls(run_id):
                raise ValueError(
                    "run has an in-flight call; inspect it before recovering"
                )
            return self.execute(run_id, plan, authorization)
        if control not in (ControlState.PAUSED, ControlState.USER_TAKEOVER):
            raise ValueError("only paused or takeover runs can resume")
        if self.database.unresolved_unknown_calls(run_id):
            raise ValueError("run has an UNKNOWN model call that must be reconciled before resume")
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
        self.database.set_run_state(run_id, RunState.COMPLETED)
        return RunResult(run_id, RunState.COMPLETED)

    def _execute_task(
        self,
        run_id: str,
        plan: PlanContract,
        authorization: AuthorizationSnapshot,
        task: TaskContract,
    ) -> None:
        independence = review_independence_decision(task)
        if not independence.allowed:
            self.database.transition_task(run_id, task.task_id, TaskState.WAITING_INPUT)
            self._complete_safe_pause(run_id, task.task_id, "stronger_review_required")
            return
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
                self._invoke_role(
                    run_id,
                    plan,
                    authorization,
                    task,
                    attempt_id,
                    number,
                    "implementation",
                    task.implementation_model,
                    worktree,
                    read_only=False,
                )
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
                self._invoke_role(
                    run_id,
                    plan,
                    authorization,
                    task,
                    attempt_id,
                    number,
                    "revision",
                    task.implementation_model,
                    worktree,
                    read_only=False,
                )
                files_ok = self._files_are_allowed(task, worktree)
                tests_ok = files_ok and self._run_tests(run_id, task, attempt_id, worktree)
                if not tests_ok:
                    self.database.complete_attempt(attempt_id, "revision_failed")
                    self._move_to_failed(run_id, task.task_id)
                    return
                self.database.transition_task(
                    run_id, task.task_id, TaskState.WAITING_REREVIEW
                )
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
    ) -> tuple[InvocationResult, ModelRef]:
        candidates = [model]
        if task.fallback_model is not None and task.fallback_model != model:
            fallback_task = replace(task, review_model=task.fallback_model)
            if not read_only or review_independence_decision(fallback_task).allowed:
                candidates.append(task.fallback_model)
        first_denial: PolicyDeniedError | None = None
        for candidate in candidates:
            call_id = str(uuid4())
            request = InvocationRequest(
                call_id=call_id,
                request_key=f"{run_id}:{task.task_id}:{attempt_number}:{role}",
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
                    "test_double": candidate.provider == "fake",
                    "on_provider_request_id": (
                        lambda provider_request_id, call_id=call_id: (
                            self.database.set_provider_request_id(
                                call_id, provider_request_id
                            )
                        )
                    ),
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
                return self._invoke_with_confirmation(request, context), candidate
            except PolicyDeniedError as error:
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
        self, request: InvocationRequest, context: InvocationContext
    ) -> InvocationResult:
        try:
            return self.invocations.invoke(request, context, confirmed=False)
        except ConfirmationRequiredError:
            if not self.confirmation_callback(request):
                raise
            return self.invocations.invoke(request, context, confirmed=True)

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
        passed = result.passed and not missing_outputs
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
        prompt = json.dumps(
            {
                "objective": task.objective,
                "acceptance_criteria": task.acceptance_criteria,
                "diff": diff,
                "deterministic_test": {
                    "passed": bool(test["passed"]),
                    "duration_ms": test["duration_ms"],
                    "evidence": json.loads(test["evidence_json"]),
                }
                if test is not None
                else None,
                "expected_response": {
                    "approved": "boolean",
                    "findings": [
                        {"severity": "P0|P1|P2|P3", "summary": "text", "evidence": "text"}
                    ],
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
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
        )
        if self.workspace.status_snapshot(worktree) != before:
            raise ReadOnlyReviewViolation("reviewer modified the worktree")
        review = self._parse_review(task, reviewer, result.output)
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
        findings = tuple(
            ReviewFinding(
                severity=Severity(item["severity"]),
                summary=str(item["summary"]),
                evidence=str(item["evidence"]),
                blocking=item["severity"] in ("P0", "P1"),
            )
            for item in data.get("findings", [])
        )
        return ReviewResult(
            review_id=str(uuid4()),
            task_id=task.task_id,
            reviewer=reviewer,
            findings=findings,
            approved=bool(data["approved"]),
        )

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
