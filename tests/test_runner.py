from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agentflow.authorization import issue_authorization
from agentflow.adapters import (
    AdapterRouter,
    InvocationIncompleteError,
    InvocationOutcomeUnknown,
    ModelUnavailableError,
)
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
from agentflow.opencode_adapter import OpenCodeAdapter
from agentflow.runner import Runner
from agentflow.states import ControlState, InvocationState, RunState, TaskState
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


def step_limit_result() -> InvocationResult:
    return InvocationResult(
        "fake-session-1",
        "CRITICAL - MAXIMUM STEPS REACHED",
        11,
        5,
        2,
        37,
        0,
        {"test_double": True},
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

    def test_untracked_output_whitespace_is_checked_beyond_git_diff_check(self) -> None:
        task = replace(
            make_task("task-1", retries=0),
            test_command=("git", "diff", "--check"),
        )
        plan = make_plan(tasks=(task,))

        def write_untracked_trailing_whitespace(
            request: InvocationRequest,
        ) -> InvocationResult:
            if request.role == "implementation":
                path = Path(request.metadata["worktree"]) / task.expected_outputs[0]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("untracked output with trailing whitespace \n")
            return InvocationResult(
                f"fake:{request.request_key}",
                "implementation complete",
                0,
                0,
                0,
                0,
                0,
                {"test_double": True},
            )

        adapter = FakeAdapter(responder=write_untracked_trailing_whitespace)
        result = Runner(self.database, adapter, self.workspace).start(
            plan,
            issue_authorization(plan),
            run_id="untracked-whitespace-run",
        )
        self.assertEqual(RunState.FAILED, result.state)
        self.assertEqual(["implementation"], [item.role for item in adapter.invocations])
        test = self.database.latest_test("untracked-whitespace-run", task.task_id)
        evidence = json.loads(test["evidence_json"])
        self.assertEqual(0, evidence["returncode"])
        self.assertTrue(evidence["untracked_whitespace_errors"])

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
            TaskState.WAITING_REVIEW,
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
                            {
                                "severity": "P1",
                                "title": "fix",
                                "explanation": "test",
                            }
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

    def test_implementation_step_limit_pauses_before_tests_or_review(self) -> None:
        task = make_task("task-1")
        plan = make_plan(tasks=(task,))
        partial = InvocationResult(
            "fake-step-limit",
            "CRITICAL - MAXIMUM STEPS REACHED",
            11,
            5,
            2,
            37,
            0,
            {"test_double": True},
        )

        def incomplete(_request: InvocationRequest) -> InvocationResult:
            raise InvocationIncompleteError(
                "step limit",
                partial,
                failure_kind="step_limit_reached",
            )

        adapter = FakeAdapter(responder=incomplete)
        runner = Runner(self.database, adapter, self.workspace)
        authorization = issue_authorization(plan)
        result = runner.start(
            plan, authorization, run_id="implementation-step-limit-run"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            TaskState.RUNNING,
            self.database.task_state("implementation-step-limit-run", task.task_id),
        )
        self.assertEqual(["implementation"], [item.role for item in adapter.invocations])
        self.assertEqual(
            0,
            self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM test_results"
            )["count"],
        )
        self.assertEqual(
            0,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )
        call = self.database.fetch_one(
            "SELECT state, input_tokens, output_tokens, duration_ms, raw_metadata_json "
            "FROM model_calls"
        )
        self.assertEqual(InvocationState.FAILED.value, call["state"])
        self.assertEqual((11, 5, 37), tuple(call[key] for key in (
            "input_tokens", "output_tokens", "duration_ms"
        )))
        self.assertEqual(
            "step_limit_reached",
            json.loads(call["raw_metadata_json"])["failure_kind"],
        )
        checkpoint = json.loads(
            self.database.run_snapshot("implementation-step-limit-run")["run"][
                "checkpoint_json"
            ]
        )
        self.assertEqual("implementation_step_limit_reached", checkpoint["reason"])
        with self.assertRaisesRegex(ValueError, "cannot be continued"):
            runner.resume("implementation-step-limit-run", plan, authorization)
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

    def test_nonzero_exit_step_limit_via_real_adapter_pauses_before_tests(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        stdout = fixture.read_text(encoding="utf-8")

        class NonZeroProcess:
            pid = 41240
            returncode = 1

            def communicate(self, timeout=None):
                return stdout, "non-zero exit"

        process = NonZeroProcess()
        captured: dict[str, object] = {}
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            if command and command[0] == "opencode-stub":
                captured["env"] = kwargs["env"]
                return process
            return real_popen(command, **kwargs)

        task = replace(
            make_task("task-1"),
            implementation_model=ModelRef(
                "lmstudio", "qwen/qwen3.8-27b", "1", "qwen", True
            ),
        )
        plan = make_plan(tasks=(task,))
        adapter = OpenCodeAdapter(opencode_command="opencode-stub")
        router = AdapterRouter({"lmstudio": adapter, "fake": FakeAdapter()})
        runner = Runner(self.database, router, self.workspace)
        authorization = issue_authorization(plan)
        with mock.patch("subprocess.Popen", side_effect=popen):
            result = runner.start(
                plan, authorization, run_id="real-adapter-step-limit-run"
            )
        self.assertEqual(RunState.PAUSED, result.state)
        snapshot = self.database.run_snapshot("real-adapter-step-limit-run")
        self.assertEqual(ControlState.PAUSED.value, snapshot["run"]["control_state"])
        checkpoint = json.loads(snapshot["run"]["checkpoint_json"])
        self.assertEqual("implementation_step_limit_reached", checkpoint["reason"])
        self.assertEqual(
            TaskState.RUNNING,
            self.database.task_state("real-adapter-step-limit-run", task.task_id),
        )
        self.assertEqual(
            0,
            self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM test_results"
            )["count"],
        )
        self.assertEqual(
            0,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )
        call = self.database.fetch_one(
            "SELECT state, provider_request_id, input_tokens, output_tokens, "
            "duration_ms, remote_cost, cost_unavailable, raw_metadata_json "
            "FROM model_calls WHERE role = 'implementation'"
        )
        self.assertEqual(InvocationState.FAILED.value, call["state"])
        self.assertEqual("session-step-limit", call["provider_request_id"])
        self.assertEqual((17, 9), (call["input_tokens"], call["output_tokens"]))
        self.assertGreaterEqual(call["duration_ms"], 0)
        self.assertEqual(0.0, call["remote_cost"])
        self.assertEqual(0, call["cost_unavailable"])
        metadata = json.loads(call["raw_metadata_json"])
        self.assertEqual("step_limit_reached", metadata["failure_kind"])
        self.assertEqual("final_text", metadata["termination_source"])
        config = json.loads(captured["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(
            task.implementation_max_steps, config["agent"]["agentflow-sandbox"]["steps"]
        )
        with self.assertRaisesRegex(ValueError, "cannot be continued"):
            runner.resume("real-adapter-step-limit-run", plan, authorization)

    def test_timeout_via_real_adapter_pauses_and_persists_unknown_usage(self) -> None:
        partial = "\n".join(
            (
                json.dumps({"type": "step_start", "sessionID": "sess-9", "part": {}}),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"tokens": {"input": 17, "output": 9}},
                    }
                ),
            )
        )

        class TimingOutProcess:
            pid = 41241
            returncode = -15
            calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(
                        ("opencode", "run"), timeout or 0, output=partial
                    )
                return "", ""

        process = TimingOutProcess()
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            if command and command[0] == "opencode-stub":
                return process
            return real_popen(command, **kwargs)

        task = replace(
            make_task("task-1"),
            implementation_model=ModelRef(
                "lmstudio", "qwen/qwen3.8-27b", "1", "qwen", True
            ),
        )
        plan = make_plan(tasks=(task,))
        adapter = OpenCodeAdapter(opencode_command="opencode-stub")
        router = AdapterRouter({"lmstudio": adapter, "fake": FakeAdapter()})
        runner = Runner(self.database, router, self.workspace)
        authorization = issue_authorization(plan)
        with mock.patch("subprocess.Popen", side_effect=popen):
            result = runner.start(plan, authorization, run_id="timeout-run")
        self.assertEqual(RunState.PAUSED, result.state)
        snapshot = self.database.run_snapshot("timeout-run")
        checkpoint = json.loads(snapshot["run"]["checkpoint_json"])
        self.assertEqual("unknown_model_call", checkpoint["reason"])
        call = self.database.fetch_one(
            "SELECT state, provider_request_id, input_tokens, output_tokens, "
            "duration_ms, remote_cost, cost_unavailable, raw_metadata_json, output_text "
            "FROM model_calls WHERE role = 'implementation'"
        )
        self.assertEqual(InvocationState.UNKNOWN.value, call["state"])
        self.assertEqual("local-process-group:41241", call["provider_request_id"])
        self.assertEqual((17, 9), (call["input_tokens"], call["output_tokens"]))
        self.assertEqual(0.0, call["remote_cost"])
        self.assertEqual(0, call["cost_unavailable"])
        self.assertEqual("", call["output_text"])
        metadata = json.loads(call["raw_metadata_json"])
        self.assertEqual("timeout", metadata["termination_reason"])
        self.assertEqual("opencode_json_events", metadata["token_source"])
        self.assertEqual("sess-9", metadata["session_id"])
        self.assertEqual(1, self.database.unresolved_unknown_calls("timeout-run"))
        with self.assertRaisesRegex(ValueError, "must be reconciled"):
            runner.resume("timeout-run", plan, authorization)


class ContinuationTests(unittest.TestCase):
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

    def _calls(self, role: str) -> list:
        return list(
            self.database.connection.execute(
                "SELECT call_id, segment_index, continuation_of_call_id, "
                "continuation_session_id, provider_request_id, state FROM model_calls "
                "WHERE role = ? ORDER BY segment_index",
                (role,),
            ).fetchall()
        )

    def test_step_limit_continues_in_same_session(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if (
                request.role == "implementation"
                and request.metadata.get("segment_index", 0) < 2
            ):
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="continuation-run")
        self.assertEqual(RunState.COMPLETED, result.state)

        implementation = [r for r in adapter.invocations if r.role == "implementation"]
        self.assertEqual(
            [0, 1, 2],
            [r.metadata.get("segment_index", 0) for r in implementation],
        )
        request_keys = [r.request_key for r in implementation]
        self.assertEqual(len(request_keys), len(set(request_keys)))
        continuations = [
            r for r in implementation if r.metadata.get("segment_index", 0) > 0
        ]
        self.assertEqual(
            ["fake-session-1", "fake-session-1"],
            [r.metadata["continuation_session_id"] for r in continuations],
        )
        self.assertEqual(
            ["segment:1", "segment:2"],
            [":".join(r.request_key.split(":")[-2:]) for r in continuations],
        )

        rows = self._calls("implementation")
        self.assertEqual([0, 1, 2], [row["segment_index"] for row in rows])
        self.assertIsNone(rows[0]["continuation_of_call_id"])
        self.assertEqual(rows[0]["call_id"], rows[1]["continuation_of_call_id"])
        self.assertEqual(rows[1]["call_id"], rows[2]["continuation_of_call_id"])
        self.assertIsNone(rows[0]["continuation_session_id"])
        self.assertEqual(rows[0]["provider_request_id"], "fake-session-1")
        self.assertEqual(rows[1]["continuation_session_id"], "fake-session-1")
        self.assertEqual(rows[2]["continuation_session_id"], "fake-session-1")
        self.assertEqual(InvocationState.COMPLETED.value, rows[2]["state"])

        scheduled = [
            event["payload_json"]
            for event in self.database.event_rows("continuation-run")
            if event["event_type"] == "continuation.scheduled"
        ]
        self.assertEqual(2, len(scheduled))
        self.assertEqual([1, 2], [json.loads(p)["segment_index"] for p in scheduled])

    def test_real_adapter_long_step_limit_continues_in_same_session(self) -> None:
        task = replace(
            make_task("task-1"),
            implementation_model=ModelRef(
                "lmstudio", "qwen/qwen3.8-27b", "1", "qwen", True
            ),
            implementation_max_continuations=1,
        )
        plan = make_plan(tasks=(task,))
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps_long.jsonl"
        step_limit_stdout = fixture.read_text(encoding="utf-8")
        session_id = "session-long-step-limit"
        done_stdout = json.dumps(
            {
                "type": "text",
                "sessionID": session_id,
                "part": {"type": "text", "text": "implementation complete"},
            }
        )

        class Process:
            pid = 41242
            returncode = 0

            def __init__(self, stdout, stderr=""):
                self.stdout = stdout
                self.stderr = stderr

            def communicate(self, timeout=None):
                return self.stdout, self.stderr

        commands: list[list[str]] = []
        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            if command and command[0] == "opencode-stub":
                commands.append(list(command))
                if "--session" in command:
                    worktree = Path(command[command.index("--dir") + 1])
                    path = worktree / task.expected_outputs[0]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("completed by continuation\n", encoding="utf-8")
                    return Process(done_stdout)
                return Process(step_limit_stdout)
            return real_popen(command, **kwargs)

        adapter = OpenCodeAdapter(opencode_command="opencode-stub")
        router = AdapterRouter(
            {"lmstudio": adapter, "fake": FakeAdapter(responder=approved_response)}
        )
        runner = Runner(self.database, router, self.workspace)
        authorization = issue_authorization(plan)
        with mock.patch("subprocess.Popen", side_effect=popen):
            result = runner.start(plan, authorization, run_id="real-cont-run")

        self.assertEqual(RunState.COMPLETED, result.state)

        base_commands = [c for c in commands if "--session" not in c]
        continuation_commands = [c for c in commands if "--session" in c]
        self.assertEqual(1, len(base_commands))
        self.assertEqual(1, len(continuation_commands))
        continuation = continuation_commands[0]
        self.assertEqual(session_id, continuation[continuation.index("--session") + 1])

        rows = self._calls("implementation")
        self.assertEqual([0, 1], [row["segment_index"] for row in rows])
        self.assertEqual(rows[0]["call_id"], rows[1]["continuation_of_call_id"])
        self.assertEqual(session_id, rows[0]["provider_request_id"])
        self.assertEqual(session_id, rows[1]["continuation_session_id"])
        self.assertEqual(InvocationState.FAILED.value, rows[0]["state"])
        self.assertEqual(InvocationState.COMPLETED.value, rows[1]["state"])

        base_metadata = json.loads(
            self.database.fetch_one(
                "SELECT raw_metadata_json FROM model_calls WHERE segment_index = 0 "
                "AND role = 'implementation'"
            )["raw_metadata_json"]
        )
        self.assertEqual("step_limit_reached", base_metadata["failure_kind"])
        self.assertEqual("final_text", base_metadata["termination_source"])

        scheduled = [
            event
            for event in self.database.event_rows("real-cont-run")
            if event["event_type"] == "continuation.scheduled"
        ]
        self.assertEqual(1, len(scheduled))
        self.assertEqual(1, json.loads(scheduled[0]["payload_json"])["segment_index"])

        self.assertEqual(
            1,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM test_results")[
                "count"
            ],
        )
        self.assertEqual(
            1,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )

    def test_step_limit_pauses_at_continuation_limit(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=1)
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        authorization = issue_authorization(plan)
        result = runner.start(plan, authorization, run_id="limit-run")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            [0, 1],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )
        checkpoint = json.loads(
            self.database.run_snapshot("limit-run")["run"]["checkpoint_json"]
        )
        self.assertEqual("implementation_step_limit_reached", checkpoint["reason"])
        with self.assertRaisesRegex(ValueError, "cannot be continued"):
            runner.resume("limit-run", plan, authorization)

    def test_step_limit_without_session_id_does_not_continue(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                raise InvocationIncompleteError(
                    "step limit",
                    InvocationResult(
                        None, "CRITICAL - MAXIMUM STEPS REACHED", 11, 5, 2, 37, 0
                    ),
                    failure_kind="step_limit_reached",
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="no-session-run")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            1,
            len([r for r in adapter.invocations if r.role == "implementation"]),
        )

    def test_continuation_allowed_rejects_remote_and_reviewer(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        local_impl = {
            "role": "implementation",
            "is_local": 1,
            "segment_index": 0,
            "provider_request_id": "session-1",
        }
        self.assertTrue(Runner._continuation_allowed(task, local_impl))
        self.assertTrue(
            Runner._continuation_allowed(task, {**local_impl, "role": "revision"})
        )
        self.assertFalse(
            Runner._continuation_allowed(task, {**local_impl, "is_local": 0})
        )
        self.assertFalse(
            Runner._continuation_allowed(task, {**local_impl, "role": "review"})
        )
        self.assertFalse(
            Runner._continuation_allowed(task, {**local_impl, "role": "rereview"})
        )
        self.assertFalse(
            Runner._continuation_allowed(task, {**local_impl, "provider_request_id": None})
        )
        self.assertFalse(
            Runner._continuation_allowed(task, {**local_impl, "provider_request_id": "bad session"})
        )
        self.assertFalse(
            Runner._continuation_allowed(task, {**local_impl, "segment_index": 2})
        )

    def test_unknown_outcome_does_not_continue(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                raise InvocationOutcomeUnknown("lost", "provider-unknown")
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="unknown-run")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            1,
            len([r for r in adapter.invocations if r.role == "implementation"]),
        )

    def test_file_scope_violation_aborts_continuation(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                path = Path(request.metadata["worktree"]) / "outside.txt"
                path.write_text("out of scope\n", encoding="utf-8")
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="scope-run")
        self.assertEqual(RunState.FAILED, result.state)
        self.assertEqual(
            1,
            len([r for r in adapter.invocations if r.role == "implementation"]),
        )

    def test_supervised_confirmation_applies_per_segment(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(RunMode.SUPERVISED, (task,))
        confirmations: list[tuple[str, int]] = []

        def responder(request: InvocationRequest) -> InvocationResult:
            if (
                request.role == "implementation"
                and request.metadata.get("segment_index", 0) < 2
            ):
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        runner = Runner(
            self.database,
            FakeAdapter(responder=responder),
            self.workspace,
            confirmation_callback=lambda request: (
                confirmations.append(
                    (request.role, int(request.metadata.get("segment_index", 0)))
                )
                or True
            ),
        )
        result = runner.start(plan, issue_authorization(plan), run_id="supervised-run")
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(
            [("implementation", 0), ("implementation", 1), ("implementation", 2), ("review", 0)],
            confirmations,
        )

    def test_resume_after_crash_between_segments_continues_once(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)

        def responder(request: InvocationRequest) -> InvocationResult:
            if (
                request.role == "implementation"
                and request.metadata.get("segment_index", 0) < 1
            ):
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        fail_call = self.database.fail_call

        def crash_after_fail(*args, **kwargs):
            fail_call(*args, **kwargs)
            raise KeyboardInterrupt("simulated process exit")

        with mock.patch.object(self.database, "fail_call", crash_after_fail):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, authorization, run_id="crash-run")
        self.assertEqual(
            1,
            len([r for r in adapter.invocations if r.role == "implementation"]),
        )
        self.database.close()
        self.database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        self.database.initialize()
        recovered = Runner(self.database, adapter, self.workspace).resume(
            "crash-run", plan, authorization
        )
        self.assertEqual(RunState.COMPLETED, recovered.state)
        self.assertEqual(
            [0, 1],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )
        self.assertEqual(2, len(self._calls("implementation")))
        scheduled = [
            event
            for event in self.database.event_rows("crash-run")
            if event["event_type"] == "continuation.scheduled"
        ]
        self.assertEqual(1, len(scheduled))

    def test_pause_request_aborts_continuation_before_next_segment(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                self.database.transition_control(
                    "pause-cont-run", ControlState.PAUSE_REQUESTED
                )
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="pause-cont-run")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            ControlState.PAUSED.value,
            self.database.run_snapshot("pause-cont-run")["run"]["control_state"],
        )
        self.assertEqual(
            [0],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )

    def test_continuation_reuses_the_actual_fallback_model(self) -> None:
        task = replace(
            make_task("task-1"),
            implementation_max_continuations=2,
            fallback_model=ModelRef("fake", "coder-fallback", "2", "coder-family", True),
        )
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role in ("review", "rereview"):
                return approved_response(request)
            if request.model.model_id == "coder-fallback":
                if request.metadata.get("segment_index", 0) < 2:
                    raise InvocationIncompleteError(
                        "step limit",
                        step_limit_result(),
                        failure_kind="step_limit_reached",
                    )
                return approved_response(request)
            raise ModelUnavailableError("primary unavailable")

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="fallback-run")
        self.assertEqual(RunState.COMPLETED, result.state)
        continuations = [
            r
            for r in adapter.invocations
            if r.role == "implementation" and r.metadata.get("segment_index", 0) > 0
        ]
        self.assertEqual([1, 2], [r.metadata.get("segment_index", 0) for r in continuations])
        self.assertTrue(all(r.model == task.fallback_model for r in continuations))
        self.assertTrue(
            all(
                r.metadata.get("continuation_session_id") == "fake-session-1"
                for r in continuations
            )
        )

    def test_session_mismatch_pauses_with_distinct_reason(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                if request.metadata.get("segment_index", 0) == 0:
                    raise InvocationIncompleteError(
                        "step limit",
                        step_limit_result(),
                        failure_kind="step_limit_reached",
                    )
                raise InvocationIncompleteError(
                    "session mismatch",
                    step_limit_result(),
                    failure_kind="session_mismatch",
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(
            plan, issue_authorization(plan), run_id="session-mismatch-run"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        checkpoint = json.loads(
            self.database.run_snapshot("session-mismatch-run")["run"]["checkpoint_json"]
        )
        self.assertEqual("session_mismatch", checkpoint["reason"])
        segment_states = [
            (row["segment_index"], row["state"])
            for row in self._calls("implementation")
        ]
        self.assertIn((1, InvocationState.FAILED.value), segment_states)
        failed = next(
            row for row in self.database.connection.execute(
                "SELECT raw_metadata_json FROM model_calls "
                "WHERE role = 'implementation' AND segment_index = 1"
            ).fetchall()
        )
        self.assertEqual(
            "session_mismatch",
            json.loads(failed["raw_metadata_json"])["failure_kind"],
        )

    def test_resume_reuses_planned_continuation_without_deadlock(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)

        def responder(request: InvocationRequest) -> InvocationResult:
            if (
                request.role == "implementation"
                and request.metadata.get("segment_index", 0) < 1
            ):
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        begin_call = self.database.begin_call
        injected: list[bool] = []

        def register_continuation_then_crash(
            request, attempt_id, *, reuse_planned=False
        ):
            if request.metadata.get("segment_index", 0) >= 1 and not injected:
                injected.append(True)
                self.database.register_call(request, attempt_id)
                raise KeyboardInterrupt("simulated crash before start")
            return begin_call(request, attempt_id, reuse_planned=reuse_planned)

        with mock.patch.object(
            self.database, "begin_call", side_effect=register_continuation_then_crash
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, authorization, run_id="planned-crash-run")

        planned = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM model_calls WHERE state = ?",
            (InvocationState.PLANNED.value,),
        )
        self.assertEqual(1, planned["count"])

        self.database.close()
        self.database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        self.database.initialize()
        recovered = Runner(self.database, adapter, self.workspace).resume(
            "planned-crash-run", plan, authorization
        )
        self.assertEqual(RunState.COMPLETED, recovered.state)
        self.assertEqual(
            [0, 1],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )
        self.assertEqual(2, len(self._calls("implementation")))
        scheduled = [
            event
            for event in self.database.event_rows("planned-crash-run")
            if event["event_type"] == "continuation.scheduled"
        ]
        self.assertEqual(1, len(scheduled))

    def test_pause_between_gate_and_continuation_start_blocks_adapter(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)

        def responder(request: InvocationRequest) -> InvocationResult:
            if (
                request.role == "implementation"
                and request.metadata.get("segment_index", 0) == 0
            ):
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        begin_call = self.database.begin_call
        injected: list[bool] = []

        def pause_before_continuation_start(
            request, attempt_id, *, reuse_planned=False
        ):
            if request.metadata.get("segment_index", 0) >= 1 and not injected:
                injected.append(True)
                self.database.transition_control(
                    "pause-toctou-run", ControlState.PAUSE_REQUESTED
                )
            return begin_call(request, attempt_id, reuse_planned=reuse_planned)

        with mock.patch.object(
            self.database, "begin_call", side_effect=pause_before_continuation_start
        ):
            result = runner.start(plan, authorization, run_id="pause-toctou-run")

        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            ControlState.PAUSED.value,
            self.database.run_snapshot("pause-toctou-run")["run"]["control_state"],
        )
        checkpoint = json.loads(
            self.database.run_snapshot("pause-toctou-run")["run"]["checkpoint_json"]
        )
        self.assertEqual("before_next_call", checkpoint["boundary"])
        self.assertEqual(
            0,
            self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM model_calls WHERE segment_index >= 1"
            )["count"],
        )
        self.assertEqual(
            0,
            self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM model_calls WHERE state = ?",
                (InvocationState.STARTED.value,),
            )["count"],
        )
        self.assertEqual(
            [0],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )

        resumed = runner.resume("pause-toctou-run", plan, authorization)
        self.assertEqual(RunState.COMPLETED, resumed.state)
        self.assertEqual(
            [0, 1],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )

    def test_resume_reuses_initial_planned_call_without_fake_continuation(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)

        def responder(request: InvocationRequest) -> InvocationResult:
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        begin_call = self.database.begin_call
        injected: list[bool] = []

        def register_base_then_crash(request, attempt_id, *, reuse_planned=False):
            if request.metadata.get("segment_index", 0) == 0 and not injected:
                injected.append(True)
                self.database.register_call(request, attempt_id)
                raise KeyboardInterrupt("simulated crash before base start")
            return begin_call(request, attempt_id, reuse_planned=reuse_planned)

        with mock.patch.object(
            self.database, "begin_call", side_effect=register_base_then_crash
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, authorization, run_id="base-planned-run")

        base = self.database.fetch_one(
            "SELECT call_id, request_key, state, segment_index, "
            "continuation_of_call_id, continuation_session_id FROM model_calls"
        )
        self.assertEqual(InvocationState.PLANNED.value, base["state"])
        self.assertEqual(0, base["segment_index"])
        self.assertIsNone(base["continuation_of_call_id"])
        self.assertIsNone(base["continuation_session_id"])
        original_call_id = base["call_id"]
        original_request_key = base["request_key"]

        self.database.close()
        self.database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        self.database.initialize()
        recovered = Runner(self.database, adapter, self.workspace).resume(
            "base-planned-run", plan, authorization
        )
        self.assertEqual(RunState.COMPLETED, recovered.state)
        self.assertEqual(
            [0],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )
        calls = self._calls("implementation")
        self.assertEqual(1, len(calls))
        self.assertEqual(original_call_id, calls[0]["call_id"])
        self.assertEqual(0, calls[0]["segment_index"])
        persisted = self.database.fetch_one(
            "SELECT request_key, continuation_of_call_id, continuation_session_id "
            "FROM model_calls WHERE call_id = ?",
            (original_call_id,),
        )
        self.assertEqual(original_request_key, persisted["request_key"])
        self.assertIsNone(persisted["continuation_of_call_id"])
        self.assertIsNone(persisted["continuation_session_id"])
        implementation_requests = [
            request for request in adapter.invocations if request.role == "implementation"
        ]
        self.assertEqual(1, len(implementation_requests))
        self.assertEqual(original_call_id, implementation_requests[0].call_id)
        self.assertEqual(original_request_key, implementation_requests[0].request_key)
        self.assertEqual(
            0,
            self.database.fetch_one(
                "SELECT COUNT(*) AS count FROM model_calls "
                "WHERE continuation_of_call_id = 'None'"
            )["count"],
        )
        scheduled = [
            event
            for event in self.database.event_rows("base-planned-run")
            if event["event_type"] == "continuation.scheduled"
        ]
        self.assertEqual([], scheduled)
        self.assertEqual(0, self.database.unfinished_calls("base-planned-run"))

    def test_malformed_planned_continuation_pauses_without_adapter(self) -> None:
        cases = [
            (
                "segment_zero_with_parent",
                lambda db, run_id, task: db.connection.execute(
                    "UPDATE model_calls SET segment_index = 0 WHERE segment_index >= 1"
                ),
            ),
            (
                "parent_string_none",
                lambda db, run_id, task: db.connection.execute(
                    "UPDATE model_calls SET continuation_of_call_id = 'None' "
                    "WHERE segment_index >= 1"
                ),
            ),
            (
                "missing_parent",
                lambda db, run_id, task: db.connection.execute(
                    "UPDATE model_calls SET continuation_of_call_id = NULL "
                    "WHERE segment_index >= 1"
                ),
            ),
            (
                "missing_session",
                lambda db, run_id, task: db.connection.execute(
                    "UPDATE model_calls SET continuation_session_id = NULL "
                    "WHERE segment_index >= 1"
                ),
            ),
            (
                "invalid_session",
                lambda db, run_id, task: db.connection.execute(
                    "UPDATE model_calls SET continuation_session_id = 'bad session' "
                    "WHERE segment_index >= 1"
                ),
            ),
            (
                "missing_parent_row",
                lambda db, run_id, task: db.connection.execute(
                    "UPDATE model_calls SET continuation_of_call_id = 'no-such-call' "
                    "WHERE segment_index >= 1"
                ),
            ),
            ("cross_attempt_parent", self._corrupt_cross_attempt_parent),
        ]
        for name, corrupt in cases:
            with self.subTest(case=name):
                self._assert_malformed_planned_pauses(name, corrupt)

    def _corrupt_cross_attempt_parent(self, db, run_id, task) -> None:
        db.create_attempt("other-attempt", run_id, task.task_id, 0)
        other = InvocationRequest(
            call_id="other-call",
            request_key="other-request-key",
            run_id=run_id,
            task_id=task.task_id,
            role="implementation",
            model=task.implementation_model,
            prompt="seed",
            data_sensitivity=task.data_sensitivity,
            read_only=False,
        )
        db.register_call(other, "other-attempt")
        db.connection.execute(
            "UPDATE model_calls SET continuation_of_call_id = 'other-call' "
            "WHERE segment_index >= 1"
        )

    def _assert_malformed_planned_pauses(self, name: str, corrupt) -> None:
        db_path = self.root / ".agentflow" / "runs" / f"malformed-{name}.db"
        database = Database(db_path)
        database.initialize()
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        run_id = f"malformed-{name}"

        def responder(request: InvocationRequest) -> InvocationResult:
            if (
                request.role == "implementation"
                and request.metadata.get("segment_index", 0) == 0
            ):
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(database, adapter, self.workspace)
        begin_call = database.begin_call
        injected: list[bool] = []

        def register_continuation_then_crash(
            request, attempt_id, *, reuse_planned=False
        ):
            if request.metadata.get("segment_index", 0) >= 1 and not injected:
                injected.append(True)
                database.register_call(request, attempt_id)
                raise KeyboardInterrupt("simulated crash before start")
            return begin_call(request, attempt_id, reuse_planned=reuse_planned)

        with mock.patch.object(
            database, "begin_call", side_effect=register_continuation_then_crash
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, authorization, run_id=run_id)

        corrupt(database, run_id, task)

        database.close()
        database = Database(db_path)
        database.initialize()
        try:
            recovered = Runner(database, adapter, self.workspace).resume(
                run_id, plan, authorization
            )
            self.assertEqual(RunState.PAUSED, recovered.state)
            self.assertEqual(
                [0],
                [
                    r.metadata.get("segment_index", 0)
                    for r in adapter.invocations
                    if r.role == "implementation"
                ],
            )
            self.assertEqual(
                0,
                database.fetch_one(
                    "SELECT COUNT(*) AS count FROM model_calls WHERE state = ?",
                    (InvocationState.STARTED.value,),
                )["count"],
            )
            checkpoint = json.loads(
                database.run_snapshot(run_id)["run"]["checkpoint_json"]
            )
            self.assertEqual("malformed_continuation_metadata", checkpoint["reason"])
        finally:
            database.close()

    def test_started_continuation_child_is_not_automatically_retried(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.role == "implementation":
                if request.metadata.get("segment_index", 0) == 0:
                    raise InvocationIncompleteError(
                        "step limit",
                        step_limit_result(),
                        failure_kind="step_limit_reached",
                    )
                raise KeyboardInterrupt("simulated crash during continuation")
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        with self.assertRaises(KeyboardInterrupt):
            runner.start(plan, authorization, run_id="started-child-run")

        child = self.database.fetch_one(
            "SELECT state FROM model_calls WHERE segment_index >= 1"
        )
        self.assertEqual(InvocationState.STARTED.value, child["state"])

        self.database.close()
        self.database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        self.database.initialize()
        with self.assertRaisesRegex(ValueError, "in-flight"):
            Runner(self.database, adapter, self.workspace).resume(
                "started-child-run", plan, authorization
            )
        self.assertEqual(
            [0, 1],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )

    def test_completed_continuation_child_is_not_reinvoked(self) -> None:
        task = replace(make_task("task-1"), implementation_max_continuations=2)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)

        def responder(request: InvocationRequest) -> InvocationResult:
            if (
                request.role == "implementation"
                and request.metadata.get("segment_index", 0) == 0
            ):
                raise InvocationIncompleteError(
                    "step limit", step_limit_result(), failure_kind="step_limit_reached"
                )
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        complete_call = self.database.complete_call

        def crash_after_completing_continuation(call_id, result, run_id):
            complete_call(call_id, result, run_id)
            row = self.database.fetch_one(
                "SELECT segment_index FROM model_calls WHERE call_id = ?", (call_id,)
            )
            if row is not None and int(row["segment_index"]) >= 1:
                raise KeyboardInterrupt("simulated crash after continuation completed")

        with mock.patch.object(
            self.database,
            "complete_call",
            side_effect=crash_after_completing_continuation,
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner.start(plan, authorization, run_id="completed-child-run")

        child = self.database.fetch_one(
            "SELECT state FROM model_calls WHERE segment_index >= 1"
        )
        self.assertEqual(InvocationState.COMPLETED.value, child["state"])

        self.database.close()
        self.database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        self.database.initialize()
        recovered = Runner(self.database, adapter, self.workspace).resume(
            "completed-child-run", plan, authorization
        )
        self.assertEqual(RunState.COMPLETED, recovered.state)
        self.assertEqual(
            [0, 1],
            [
                r.metadata.get("segment_index", 0)
                for r in adapter.invocations
                if r.role == "implementation"
            ],
        )

    def test_revision_retries_to_max_retry_count(self) -> None:
        task = replace(make_task("task-1", retries=2), implementation_max_continuations=0)
        plan = make_plan(tasks=(task,))
        revisions = 0

        def responder(request: InvocationRequest) -> InvocationResult:
            nonlocal revisions
            if request.role == "implementation":
                return InvocationResult(
                    f"fake:{request.request_key}", "implemented", 0, 0, 0, 0, 0,
                    {"test_double": True},
                )
            if request.role == "revision":
                revisions += 1
                if revisions < 2:
                    return InvocationResult(
                        f"fake:{request.request_key}", "still broken", 0, 0, 0, 0, 0,
                        {"test_double": True},
                    )
                return approved_response(request)
            return approved_response(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="revision-run")
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(
            ["implementation", "revision", "revision", "rereview"],
            [r.role for r in adapter.invocations],
        )


if __name__ == "__main__":
    unittest.main()
