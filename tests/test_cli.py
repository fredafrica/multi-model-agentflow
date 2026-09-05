from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from agentflow.cli import main
from agentflow.contracts import (
    BudgetMode,
    BusinessImportance,
    DataSensitivity,
    ModelRef,
    OperationalSafety,
    PlanContract,
    RiskLevel,
    RunMode,
    TaskContract,
    InvocationRequest,
)
from agentflow.database import Database
from agentflow.serialization import canonical_json, plan_hash
from agentflow.states import InvocationState, TaskState


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def cli_plan() -> PlanContract:
    task = TaskContract(
        task_id="no-op",
        objective="Validate the CLI without writing files",
        risk_level=RiskLevel(
            BusinessImportance.NORMAL,
            OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE,
        ),
        allowed_files=("noop.txt",),
        forbidden_actions=("network",),
        acceptance_criteria=("deterministic gate passes",),
        data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
        implementation_model=ModelRef("fake", "coder", "1", "coder", True),
        review_model=ModelRef("fake", "reviewer", "1", "reviewer", True),
        fallback_model=None,
        max_remote_cost=0,
        max_retry_count=0,
        escalation_conditions=("failure",),
        expected_outputs=(),
    )
    return PlanContract(
        plan_id="cli-plan",
        schema_version=1,
        version=1,
        run_mode=RunMode.MANAGED,
        budget_mode=BudgetMode.LOCAL_FREE,
        max_remote_cost=0,
        emergency_reserve=0,
        privacy_policy_version="1",
        tasks=(task,),
    )


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / ".gitignore").write_text(".agentflow/runs/\n", encoding="utf-8")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", ".gitignore", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        self.plan = cli_plan()
        policy = self.root / ".agentflow"
        policy.mkdir()
        (policy / "plan.json").write_text(canonical_json(self.plan), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--project", str(self.root), *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_plan_requires_matching_hash_then_completes(self) -> None:
        code, output, _ = self.call("plan", "show")
        self.assertEqual(0, code)
        self.assertEqual(plan_hash(self.plan), json.loads(output)["sha256"])

        code, _, error = self.call("plan", "authorize", "--hash", "wrong")
        self.assertEqual(2, code)
        self.assertIn("does not match", error)

        code, output, _ = self.call(
            "plan", "authorize", "--hash", plan_hash(self.plan)
        )
        self.assertEqual(0, code)
        self.assertEqual(plan_hash(self.plan), json.loads(output)["plan_hash"])

        code, output, error = self.call(
            "start", "cli-plan", "--run-id", "cli-run"
        )
        self.assertEqual(0, code, error)
        self.assertEqual("completed", json.loads(output)["state"])

        database_path = self.root / ".agentflow" / "runs" / "agentflow.db"
        connection = sqlite3.connect(database_path)
        try:
            before = connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
        finally:
            connection.close()

        code, output, _ = self.call("status", "cli-run", "--watch")
        self.assertEqual(0, code)
        self.assertEqual("completed", json.loads(output)["run"]["run_state"])

        code, output, _ = self.call("cost", "cli-run")
        self.assertEqual(0, code)
        self.assertEqual(0, json.loads(output)["usd"])

        code, output, _ = self.call("logs", "cli-run")
        self.assertEqual(0, code)
        events = [json.loads(line)["event_type"] for line in output.splitlines()]
        self.assertIn("call.completed", events)
        self.assertIn("review.recorded", events)

        code, output, _ = self.call("handoff", "cli-run")
        self.assertEqual(0, code)
        self.assertEqual(plan_hash(self.plan), json.loads(output)["plan_hash"])

        connection = sqlite3.connect(database_path)
        try:
            after = connection.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0]
            call = connection.execute(
                """
                SELECT data_sensitivity, read_only, request_scope_json, output_text,
                       input_tokens, output_tokens, duration_ms, remote_cost
                FROM model_calls WHERE role = 'review'
                """
            ).fetchone()
            tests = connection.execute("SELECT COUNT(*) FROM test_results").fetchone()[0]
            reviews = connection.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(before, after)
        self.assertEqual("D1", call[0])
        self.assertEqual(1, call[1])
        self.assertNotIn("prompt", json.loads(call[2]))
        self.assertTrue(call[3])
        self.assertEqual(1, tests)
        self.assertEqual(1, reviews)

    def test_immediate_pause_and_cancel_terminate_active_opencode_group(self) -> None:
        for action, expected_run_state in (
            ("cancel", "cancelled"),
            ("pause", "paused"),
        ):
            with self.subTest(action=action):
                code, _, error = self.call(
                    "plan", "authorize", "--hash", plan_hash(self.plan)
                )
                self.assertEqual(0, code, error)
                run_id = f"{action}-run"
                attempt_id = f"{action}-attempt"
                call_id = f"{action}-call"
                process_id = 4242 if action == "pause" else 4243
                process_reference = f"local-process-group:{process_id}"
                database = Database(
                    self.root / ".agentflow" / "runs" / "agentflow.db"
                )
                database.initialize()
                try:
                    authorization = database.latest_authorization(
                        self.plan.plan_id, self.plan.version
                    )
                    database.create_run(run_id, self.plan, authorization)
                    database.transition_task(
                        run_id, "no-op", TaskState.WAITING_AUTHORIZATION
                    )
                    database.transition_task(run_id, "no-op", TaskState.QUEUED)
                    database.transition_task(run_id, "no-op", TaskState.RUNNING)
                    database.create_attempt(attempt_id, run_id, "no-op", 1)
                    request = InvocationRequest(
                        call_id=call_id,
                        request_key=f"{action}-request",
                        run_id=run_id,
                        task_id="no-op",
                        role="implementation",
                        model=ModelRef(
                            "lmstudio", "local-model", "1", "local", True
                        ),
                        prompt="test",
                        data_sensitivity=DataSensitivity.PUBLIC,
                        read_only=False,
                    )
                    database.register_call(request, attempt_id)
                    database.transition_call(call_id, InvocationState.STARTED)
                    database.set_provider_request_id(call_id, process_reference)
                finally:
                    database.close()
                with mock.patch(
                    "agentflow.cli.OpenCodeAdapter.cancel", return_value=True
                ) as cancel:
                    arguments = (
                        ("pause", run_id, "--immediate")
                        if action == "pause"
                        else ("cancel", run_id)
                    )
                    code, output, error = self.call(*arguments)
                self.assertEqual(0, code, error)
                cancel.assert_called_once_with(process_reference)
                self.assertEqual(
                    expected_run_state,
                    json.loads(output)["run"]["run_state"],
                )
                connection = sqlite3.connect(
                    self.root / ".agentflow" / "runs" / "agentflow.db"
                )
                try:
                    state = connection.execute(
                        "SELECT state FROM model_calls WHERE call_id = ?",
                        (call_id,),
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual("unknown", state)


if __name__ == "__main__":
    unittest.main()
