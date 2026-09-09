"""Run/plan/authorization/cancel/dispatch consistency regression tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentflow.adapters import AdapterRouter
from agentflow.authorization import issue_authorization
from agentflow.contracts import (
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
from agentflow.service import InvocationService
from agentflow.states import ControlState, InvocationState, RunState, TaskState
from agentflow.workspace import GitWorkspace
from resource_budget_fixtures import budgeted_plan_contract


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def make_task(task_id: str = "task-1") -> TaskContract:
    relative = f"outputs/{task_id}.txt"
    return TaskContract(
        task_id=task_id,
        objective=f"Create {relative}",
        risk_level=RiskLevel(
            BusinessImportance.NORMAL, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE
        ),
        allowed_files=(relative,),
        forbidden_actions=("network",),
        acceptance_criteria=(f"{relative} exists",),
        data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
        implementation_model=ModelRef("fake", "coder", "1", "coder-family", True),
        review_model=ModelRef("fake", "reviewer", "1", "reviewer-family", True),
        fallback_model=None,
        max_remote_cost=0,
        max_retry_count=1,
        escalation_conditions=("test failure",),
        expected_outputs=(relative,),
        test_command=("/bin/sh", "-c", f"test -f {relative}"),
    )


def make_plan(tasks: tuple[TaskContract, ...]) -> PlanContract:
    return budgeted_plan_contract(
        plan_id="runner-plan",
        schema_version=1,
        version=1,
        run_mode=RunMode.MANAGED,
        budget_mode="LOCAL_FREE",
        max_remote_cost=0,
        emergency_reserve=0,
        privacy_policy_version="1",
        tasks=tasks,
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


def request_for(task: TaskContract, key: str = "request-1") -> InvocationRequest:
    return InvocationRequest(
        call_id=f"call-{key}",
        request_key=key,
        run_id="run-1",
        task_id=task.task_id,
        role="implementation",
        model=task.implementation_model,
        prompt="public test input",
        data_sensitivity=task.data_sensitivity,
        read_only=False,
    )


class TerminalDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "agentflow.db")
        self.database.initialize()
        self.task = make_task("task-1")
        self.other_task = make_task("task-2")
        self.plan = make_plan((self.task, self.other_task))
        self.authorization = issue_authorization(self.plan)
        self.database.save_plan(self.plan)
        self.database.save_authorization(self.authorization)
        self.database.create_run("run-1", self.plan, self.authorization)
        self.database.create_attempt("attempt-1", "run-1", self.task.task_id, 1)
        self.database.create_attempt("attempt-2", "run-1", self.other_task.task_id, 1)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_begin_call_blocked_after_terminal_run(self) -> None:
        self.database.finalize_run(
            "run-1", RunState.CANCELLED, "run_cancelled", {},
            max_chars=6000, max_checkpoints=1,
        )
        request = request_for(self.task)
        row, outcome = self.database.begin_call(request, "attempt-1")
        self.assertIsNone(row)
        self.assertEqual("PAUSE_BLOCKED", outcome)
        self.assertEqual(
            (), self.database.active_provider_requests("run-1")
        )

    def test_terminal_run_state_is_irreversible(self) -> None:
        self.database.set_run_state("run-1", RunState.COMPLETED)
        with self.assertRaises(ValueError):
            self.database.set_run_state("run-1", RunState.RUNNING)
        with self.assertRaises(ValueError):
            self.database.set_run_state("run-1", RunState.PAUSED)

    def test_control_transition_blocked_after_terminal(self) -> None:
        self.database.set_run_state("run-1", RunState.CANCELLED)
        with self.assertRaises(ValueError):
            self.database.transition_control("run-1", ControlState.PAUSE_REQUESTED)

    def test_begin_call_validates_attempt_ownership(self) -> None:
        request = request_for(self.task)
        with self.assertRaises(ValueError):
            self.database.begin_call(request, "attempt-2")


class ResumeConsistencyTests(unittest.TestCase):
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

    def test_resume_rejects_different_plan_without_calls(self) -> None:
        task = make_task("one")
        old = make_plan((task,))
        auth = issue_authorization(old)
        adapter = FakeAdapter(responder=approved_response)
        runner = Runner(
            self.database, adapter, self.workspace,
            confirmation_callback=lambda request: False,
        )
        runner.start(old, auth, run_id="foreign")
        calls = len(adapter.invocations)
        new = replace(old, plan_id="different-plan", run_mode=RunMode.SUPERVISED)
        self.database.save_plan(new)
        new_auth = issue_authorization(new)
        self.database.save_authorization(new_auth)
        with self.assertRaises(ValueError):
            runner.resume("foreign", new, new_auth)
        self.assertEqual(calls, len(adapter.invocations))
        self.assertEqual(
            "runner-plan",
            self.database.run_snapshot("foreign")["run"]["plan_id"],
        )

    def test_resume_rejects_terminal_run(self) -> None:
        task = make_task("one")
        plan = make_plan((task,))
        adapter = FakeAdapter(responder=approved_response)
        runner = Runner(self.database, adapter, self.workspace)
        result = runner.start(plan, issue_authorization(plan), run_id="done")
        self.assertEqual(RunState.COMPLETED, result.state)
        with self.assertRaises(ValueError):
            runner.resume("done", plan, issue_authorization(plan))

    def test_duplicate_resume_does_not_reinvoke(self) -> None:
        task = make_task("one")
        plan = make_plan((task,))
        adapter = FakeAdapter(responder=approved_response)
        runner = Runner(self.database, adapter, self.workspace)
        auth = issue_authorization(plan)
        first = runner.start(plan, auth, run_id="idem")
        self.assertEqual(RunState.COMPLETED, first.state)
        calls = len(adapter.invocations)
        with self.assertRaises(ValueError):
            runner.resume("idem", plan, auth)
        self.assertEqual(calls, len(adapter.invocations))


class ResolveUnknownRoleRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "agentflow.db")
        self.database.initialize()
        self.task = make_task()
        self.plan = make_plan((self.task,))
        self.authorization = issue_authorization(self.plan)
        self.database.save_plan(self.plan)
        self.database.save_authorization(self.authorization)
        self.database.create_run("run-1", self.plan, self.authorization)
        self.database.create_attempt("attempt-1", "run-1", self.task.task_id, 1)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_resolve_unknown_routes_by_role(self) -> None:
        review_model = ModelRef("shared", "reviewer", "1", "family-b", False)
        request = InvocationRequest(
            call_id="call-review",
            request_key="review-1",
            run_id="run-1",
            task_id=self.task.task_id,
            role="review",
            model=review_model,
            prompt="public test input",
            data_sensitivity=self.task.data_sensitivity,
            read_only=True,
        )
        self.database.register_call(request, "attempt-1")
        self.database.transition_call("call-review", InvocationState.STARTED)
        result = InvocationResult("pr-1", "partial", 1, 2, 3, 4, None)
        self.database.mark_call_unknown("call-review", "pr-1", result)

        queried: list[str] = []

        class RecordingAdapter:
            def __init__(self, name: str) -> None:
                self.name = name

            def query(self, provider_request_id: str):
                queried.append(self.name)
                return None

        impl = RecordingAdapter("impl")
        reviewer = RecordingAdapter("reviewer")
        router = AdapterRouter(
            {"shared": impl},
            role_adapters={("shared", "review"): reviewer},
        )
        service = InvocationService(self.database, router)
        self.assertIsNone(service.resolve_unknown("run-1", "call-review"))
        self.assertEqual(["reviewer"], queried)


if __name__ == "__main__":
    unittest.main()
