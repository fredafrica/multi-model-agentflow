from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agentflow.authorization import issue_authorization
from agentflow.adapters import AdapterRouter, InvocationOutcomeUnknown
from agentflow.contracts import (
    BudgetMode,
    BusinessImportance,
    DataSensitivity,
    InvocationRequest,
    InvocationResult,
    ModelRef,
    OperationalSafety,
    PlanContract,
    RiskLevel,
    RunMode,
    TaskContract,
)
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.runner import Runner
from agentflow.states import ControlState, RunState, TaskState
from agentflow.workspace import GitWorkspace, GitWorkspaceError


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def make_task(
    task_id: str,
    *,
    retries: int = 1,
    importance: BusinessImportance = BusinessImportance.NORMAL,
    depends_on: tuple[str, ...] = (),
) -> TaskContract:
    relative = f"outputs/{task_id}.txt"
    return TaskContract(
        task_id=task_id,
        objective=f"Create {relative}",
        risk_level=RiskLevel(
            importance,
            OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE,
        ),
        allowed_files=(relative,),
        forbidden_actions=("network",),
        acceptance_criteria=(f"{relative} exists",),
        data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
        implementation_model=ModelRef("fake", "coder", "1", "coder-family", True),
        review_model=ModelRef("fake", "reviewer", "1", "reviewer-family", True),
        fallback_model=None,
        max_remote_cost=0,
        max_retry_count=retries,
        escalation_conditions=("test failure",),
        expected_outputs=(relative,),
        test_command=("/bin/sh", "-c", f"test -f {relative}"),
        depends_on=depends_on,
    )


def make_plan(mode: RunMode = RunMode.MANAGED, tasks: tuple[TaskContract, ...] | None = None):
    actual_tasks = tasks or tuple(make_task(f"task-{number}") for number in range(1, 4))
    return PlanContract(
        plan_id="runner-plan",
        schema_version=1,
        version=1,
        run_mode=mode,
        budget_mode=BudgetMode.LOCAL_FREE,
        max_remote_cost=0,
        emergency_reserve=0,
        privacy_policy_version="1",
        tasks=actual_tasks,
    )


def approved_response(request: InvocationRequest) -> InvocationResult:
    if request.role in ("implementation", "revision"):
        path = Path(request.metadata["worktree"]) / request.metadata["allowed_files"][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"created by {request.role}\n", encoding="utf-8")
        output = "implementation complete"
    else:
        output = json.dumps({"approved": True, "findings": []})
    return InvocationResult(
        provider_request_id=f"fake:{request.request_key}",
        output=output,
        input_tokens=0,
        output_tokens=0,
        first_token_latency_ms=0,
        duration_ms=0,
        remote_cost=0,
        raw_metadata={"test_double": True},
    )


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        runs = self.root / ".agentflow" / "runs"
        self.database = Database(runs / "agentflow.db")
        self.database.initialize()
        self.workspace = GitWorkspace(self.root, runs)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_three_task_plan_completes_in_serial_worktrees(self) -> None:
        plan = make_plan()
        adapter = FakeAdapter(responder=approved_response)
        result = Runner(self.database, adapter, self.workspace).start(
            plan, issue_authorization(plan), run_id="serial-run"
        )
        snapshot = self.database.run_snapshot(result.run_id)
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual([TaskState.APPROVED.value] * 3, [item["state"] for item in snapshot["tasks"]])
        self.assertEqual(6, len(adapter.invocations))
        self.assertEqual(
            ["implementation", "review"] * 3,
            [request.role for request in adapter.invocations],
        )
        self.assertEqual(3, len({item["worktree_path"] for item in snapshot["tasks"]}))

    def test_supervised_mode_confirms_every_invocation(self) -> None:
        task = make_task("task-1")
        plan = make_plan(RunMode.SUPERVISED, (task,))
        confirmations: list[str] = []
        runner = Runner(
            self.database,
            FakeAdapter(responder=approved_response),
            self.workspace,
            confirmation_callback=lambda request: confirmations.append(request.role) or True,
        )
        result = runner.start(plan, issue_authorization(plan), run_id="supervised-run")
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(["implementation", "review"], confirmations)

    def test_declined_supervised_confirmation_pauses_at_resumable_boundary(self) -> None:
        task = make_task("task-1")
        plan = make_plan(RunMode.SUPERVISED, (task,))
        result = Runner(
            self.database,
            FakeAdapter(responder=approved_response),
            self.workspace,
            confirmation_callback=lambda _request: False,
        ).start(plan, issue_authorization(plan), run_id="declined-run")
        snapshot = self.database.run_snapshot("declined-run")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(ControlState.PAUSED.value, snapshot["run"]["control_state"])

    def test_adaptive_mode_confirms_important_invocations(self) -> None:
        task = make_task("task-1", importance=BusinessImportance.IMPORTANT)
        plan = make_plan(RunMode.ADAPTIVE, (task,))
        confirmations: list[str] = []
        result = Runner(
            self.database,
            FakeAdapter(responder=approved_response),
            self.workspace,
            confirmation_callback=lambda request: confirmations.append(request.role) or True,
        ).start(plan, issue_authorization(plan), run_id="adaptive-run")
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(["implementation", "review"], confirmations)

    def test_safe_pause_after_invocation_and_resume(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))

        def pause_after_implementation(request: InvocationRequest) -> InvocationResult:
            result = approved_response(request)
            if request.role == "implementation":
                self.database.transition_control("pause-run", ControlState.PAUSE_REQUESTED)
            return result

        runner = Runner(
            self.database, FakeAdapter(responder=pause_after_implementation), self.workspace
        )
        authorization = issue_authorization(plan)
        paused = runner.start(plan, authorization, run_id="pause-run")
        self.assertEqual(RunState.PAUSED, paused.state)
        self.assertEqual(TaskState.SELF_TESTING, self.database.task_state("pause-run", "task-1"))
        resumed = runner.resume("pause-run", plan, authorization)
        self.assertEqual(RunState.COMPLETED, resumed.state)

    def test_file_scope_violation_fails_run(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))

        def write_outside(request: InvocationRequest) -> InvocationResult:
            result = approved_response(request)
            if request.role == "implementation":
                (Path(request.metadata["worktree"]) / "outside.txt").write_text("bad")
            return result

        result = Runner(
            self.database, FakeAdapter(responder=write_outside), self.workspace
        ).start(plan, issue_authorization(plan), run_id="scope-run")
        self.assertEqual(RunState.FAILED, result.state)

    def test_missing_expected_output_fails_even_when_command_passes(self) -> None:
        task = replace(make_task("task-1"), test_command=())
        plan = make_plan(tasks=(task,))

        def no_write(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                f"fake:{request.request_key}", "done", 0, 0, 0, 0, 0
            )

        result = Runner(
            self.database, FakeAdapter(responder=no_write), self.workspace
        ).start(plan, issue_authorization(plan), run_id="missing-output-run")
        self.assertEqual(RunState.FAILED, result.state)

    def test_uncommitted_project_does_not_leave_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            git(root, "init", "-b", "main")
            database = Database(root / ".agentflow" / "runs" / "agentflow.db")
            database.initialize()
            task = make_task("task-1")
            plan = make_plan(tasks=(task,))
            try:
                with self.assertRaises(GitWorkspaceError):
                    Runner(
                        database,
                        FakeAdapter(responder=approved_response),
                        GitWorkspace(root, root / ".agentflow" / "runs"),
                    ).start(plan, issue_authorization(plan), run_id="no-head-run")
                count = database.fetch_one("SELECT COUNT(*) AS count FROM runs")
                self.assertEqual(0, count["count"])
            finally:
                database.close()

    def test_local_free_uses_authorized_local_fallback(self) -> None:
        base = make_task("task-1")
        remote_model = ModelRef("remote", "coder", "1", "remote-family", False)
        local_fallback = ModelRef("fake", "fallback", "1", "fallback-family", True)
        task = replace(
            base,
            implementation_model=remote_model,
            fallback_model=local_fallback,
        )
        plan = make_plan(tasks=(task,))
        remote = FakeAdapter(responder=approved_response)
        local = FakeAdapter(responder=approved_response)
        adapter = AdapterRouter({"remote": remote, "fake": local})
        result = Runner(self.database, adapter, self.workspace).start(
            plan, issue_authorization(plan), run_id="fallback-run"
        )
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual([], remote.invocations)
        self.assertEqual(
            ["implementation", "review"],
            [request.role for request in local.invocations],
        )
        events = self.database.event_rows("fallback-run")
        self.assertIn(
            "model.fallback_selected", [event["event_type"] for event in events]
        )

    def test_missing_independent_reviewer_waits_instead_of_approving(self) -> None:
        task = replace(
            make_task("task-1", importance=BusinessImportance.IMPORTANT),
            review_model=ModelRef(
                "fake", "reviewer", "1", "coder-family", True
            ),
        )
        plan = make_plan(tasks=(task,))
        result = Runner(
            self.database, FakeAdapter(responder=approved_response), self.workspace
        ).start(plan, issue_authorization(plan), run_id="review-wait-run")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            TaskState.WAITING_INPUT,
            self.database.task_state("review-wait-run", task.task_id),
        )

    def test_reviewer_write_fails_run(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))

        def reviewer_writes(request: InvocationRequest) -> InvocationResult:
            result = approved_response(request)
            if request.role == "review":
                path = Path(request.metadata["worktree"]) / task.allowed_files[0]
                path.write_text("reviewer mutation\n")
            return result

        result = Runner(
            self.database, FakeAdapter(responder=reviewer_writes), self.workspace
        ).start(plan, issue_authorization(plan), run_id="review-write-run")
        self.assertEqual(RunState.FAILED, result.state)

    def test_p1_is_revised_and_rereviewed(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))
        review_count = 0

        def reject_once(request: InvocationRequest) -> InvocationResult:
            nonlocal review_count
            if request.role in ("implementation", "revision"):
                return approved_response(request)
            review_count += 1
            if review_count == 1:
                output = json.dumps(
                    {
                        "approved": False,
                        "findings": [
                            {"severity": "P1", "summary": "fix", "evidence": "test"}
                        ],
                    }
                )
                return InvocationResult(
                    f"fake:{request.request_key}", output, 0, 0, 0, 0, 0, {"test_double": True}
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=reject_once)
        result = Runner(self.database, adapter, self.workspace).start(
            plan, issue_authorization(plan), run_id="revision-run"
        )
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(
            ["implementation", "review", "revision", "rereview"],
            [request.role for request in adapter.invocations],
        )

    def test_takeover_change_invalidates_only_affected_task(self) -> None:
        first = make_task("task-1")
        second = make_task("task-2")
        plan = make_plan(tasks=(first, second))
        authorization = issue_authorization(plan)

        def pause_after_last_review(request: InvocationRequest) -> InvocationResult:
            result = approved_response(request)
            if request.task_id == second.task_id and request.role == "review":
                self.database.transition_control(
                    "takeover-run", ControlState.PAUSE_REQUESTED
                )
            return result

        adapter = FakeAdapter(responder=pause_after_last_review)
        runner = Runner(self.database, adapter, self.workspace)
        paused = runner.start(plan, authorization, run_id="takeover-run")
        self.assertEqual(RunState.PAUSED, paused.state)
        original_invocations = len(adapter.invocations)

        self.database.transition_control("takeover-run", ControlState.USER_TAKEOVER)
        snapshot = self.database.run_snapshot("takeover-run")
        first_worktree = Path(snapshot["tasks"][0]["worktree_path"])
        (first_worktree / first.allowed_files[0]).write_text("user modification\n")

        resumed = runner.resume("takeover-run", plan, authorization)
        self.assertEqual(RunState.COMPLETED, resumed.state)
        new_roles = [request.role for request in adapter.invocations[original_invocations:]]
        self.assertEqual(["review"], new_roles)

    def test_takeover_change_also_invalidates_dependent_task(self) -> None:
        first = make_task("task-1")
        second = make_task("task-2", depends_on=("task-1",))
        plan = make_plan(tasks=(first, second))
        authorization = issue_authorization(plan)
        pause_sent = False

        def pause_after_last_review(request: InvocationRequest) -> InvocationResult:
            nonlocal pause_sent
            result = approved_response(request)
            if (
                not pause_sent
                and request.task_id == second.task_id
                and request.role == "review"
            ):
                pause_sent = True
                self.database.transition_control(
                    "dependent-run", ControlState.PAUSE_REQUESTED
                )
            return result

        adapter = FakeAdapter(responder=pause_after_last_review)
        runner = Runner(self.database, adapter, self.workspace)
        paused = runner.start(plan, authorization, run_id="dependent-run")
        self.assertEqual(RunState.PAUSED, paused.state)
        original_invocations = len(adapter.invocations)

        self.database.transition_control("dependent-run", ControlState.USER_TAKEOVER)
        snapshot = self.database.run_snapshot("dependent-run")
        first_worktree = Path(snapshot["tasks"][0]["worktree_path"])
        (first_worktree / first.allowed_files[0]).write_text("user modification\n")
        resumed = runner.resume("dependent-run", plan, authorization)

        self.assertEqual(RunState.COMPLETED, resumed.state)
        self.assertEqual(
            ["review", "review"],
            [request.role for request in adapter.invocations[original_invocations:]],
        )

    def test_immediate_freeze_can_enter_takeover(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        self.database.save_plan(plan)
        self.database.save_authorization(authorization)
        self.database.create_run("freeze-run", plan, authorization)
        self.database.force_pause("freeze-run")
        self.database.transition_control("freeze-run", ControlState.USER_TAKEOVER)
        snapshot = self.database.run_snapshot("freeze-run")
        self.assertEqual(ControlState.USER_TAKEOVER.value, snapshot["run"]["control_state"])

    def test_unknown_invocation_pauses_and_cannot_resume(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))

        def unknown(_request: InvocationRequest) -> InvocationResult:
            raise InvocationOutcomeUnknown("response lost", "provider-request")

        adapter = FakeAdapter(responder=unknown)
        runner = Runner(self.database, adapter, self.workspace)
        authorization = issue_authorization(plan)
        result = runner.start(plan, authorization, run_id="unknown-run")
        self.assertEqual(RunState.PAUSED, result.state)
        with self.assertRaises(ValueError):
            runner.resume("unknown-run", plan, authorization)
        self.assertEqual(1, len(adapter.invocations))

    def test_resolved_unknown_call_resumes_without_second_invocation(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))

        def lost_after_write(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                path = Path(request.metadata["worktree"]) / task.allowed_files[0]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("completed before response was lost\n", encoding="utf-8")
                raise InvocationOutcomeUnknown("response lost", "provider-resolved")
            return approved_response(request)

        adapter = FakeAdapter(responder=lost_after_write)
        runner = Runner(self.database, adapter, self.workspace)
        authorization = issue_authorization(plan)
        paused = runner.start(plan, authorization, run_id="resolved-run")
        self.assertEqual(RunState.PAUSED, paused.state)
        adapter.results["provider-resolved"] = InvocationResult(
            "provider-resolved", "implementation complete", 1, 1, 1, 1, 0
        )
        call = self.database.fetch_one(
            "SELECT call_id FROM model_calls WHERE provider_request_id = ?",
            ("provider-resolved",),
        )
        runner.invocations.resolve_unknown("resolved-run", call["call_id"])
        resumed = runner.resume("resolved-run", plan, authorization)
        self.assertEqual(RunState.COMPLETED, resumed.state)
        self.assertEqual(
            ["implementation", "review"],
            [request.role for request in adapter.invocations],
        )

    def test_restart_after_completed_call_reuses_result(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        adapter = FakeAdapter(responder=approved_response)
        runner = Runner(self.database, adapter, self.workspace)
        complete_call = self.database.complete_call

        def crash_after_save(*args, **kwargs):
            complete_call(*args, **kwargs)
            raise KeyboardInterrupt("simulated process exit")

        with mock.patch.object(self.database, "complete_call", crash_after_save):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, authorization, run_id="restart-run")
        self.database.close()
        self.database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        self.database.initialize()
        recovered = Runner(self.database, adapter, self.workspace).resume(
            "restart-run", plan, authorization
        )
        self.assertEqual(RunState.COMPLETED, recovered.state)
        self.assertEqual(
            ["implementation", "review"],
            [request.role for request in adapter.invocations],
        )

    def test_handoff_summary_survives_database_reopen(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))

        def pause_after_implementation(request: InvocationRequest) -> InvocationResult:
            result = approved_response(request)
            if request.role == "implementation":
                self.database.transition_control(
                    "handoff-run", ControlState.PAUSE_REQUESTED
                )
            return result

        Runner(
            self.database,
            FakeAdapter(responder=pause_after_implementation),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="handoff-run")
        before = self.database.handoff_summary("handoff-run")
        self.database.close()
        self.database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        self.database.initialize()
        after = self.database.handoff_summary("handoff-run")
        self.assertEqual(before, after)
        self.assertTrue(Path(after["tasks"][0]["worktree_path"]).exists())


if __name__ == "__main__":
    unittest.main()
