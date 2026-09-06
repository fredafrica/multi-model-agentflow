"""Remote worker data-boundary and fallback sandbox regression tests."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentflow.adapters import AdapterRouter, ModelUnavailableError
from agentflow.authorization import issue_authorization
from agentflow.contracts import (
    BudgetMode,
    DataSensitivity,
    InputArtifact,
    InvocationRequest,
    InvocationResult,
    ModelRef,
)
from agentflow.fake_adapter import FakeAdapter
from agentflow.runner import Runner
from agentflow.states import RunState, TaskState
from agentflow.workspace import GitWorkspace

from test_remote_worker import WORKER_PROVIDER, WORKER_MODEL, worker_plan, worker_task
from test_runner import make_task, make_plan


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def _approve(request: InvocationRequest) -> InvocationResult:
    return InvocationResult(
        f"fake:{request.request_key}",
        json.dumps({"approved": True, "findings": []}),
        0, 0, 0, 0, 0,
        {"test_double": True},
    )


def _implement(request: InvocationRequest) -> InvocationResult:
    path = Path(request.metadata["worktree"]) / request.metadata["allowed_files"][0]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"created by {request.role}\n", encoding="utf-8")
    return InvocationResult(
        f"fake:{request.request_key}", "implementation complete", 0, 0, 0, 0, 0,
        {"test_double": True},
    )


class RemoteBoundaryTests(unittest.TestCase):
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
        self.database = self._open_db(runs)
        self.workspace = GitWorkspace(self.root, runs)

    def _open_db(self, runs: Path):
        from agentflow.database import Database

        db = Database(runs / "agentflow.db")
        db.initialize()
        return db

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _worker(self, responder):
        return FakeAdapter(responder=responder)

    def _runner(self, worker):
        return Runner(
            self.database,
            AdapterRouter(
                {"fake": FakeAdapter(responder=_approve), WORKER_PROVIDER: worker}
            ),
            self.workspace,
        )

    def test_input_artifact_with_secret_blocks_remote_call(self) -> None:
        content = b"api_key=FAKE_AUDIT_SECRET_NOT_A_REAL_KEY\n"
        (self.root / "input.txt").write_bytes(content)
        task = worker_task(
            input_artifacts=(InputArtifact("input.txt", hashlib.sha256(content).hexdigest()),)
        )
        plan = worker_plan(task)
        worker = self._worker(_implement)
        result = self._runner(worker).start(
            plan, issue_authorization(plan), run_id="secret-input-run"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker.invocations))
        pending = self.database.pending_supervisor_checkpoints("secret-input-run")
        self.assertTrue(any(item["reason"] == "privacy_violation" for item in pending))

    def test_existing_allowed_file_with_secret_blocks_remote_call(self) -> None:
        task = worker_task()
        relative = task.allowed_files[0]
        (self.root / relative).parent.mkdir(parents=True, exist_ok=True)
        (self.root / relative).write_text(
            "api_key=FAKE_AUDIT_SECRET_NOT_A_REAL_KEY\n", encoding="utf-8"
        )
        git(self.root, "add", relative)
        git(self.root, "commit", "-m", "pre-existing output")
        plan = worker_plan(task)
        worker = self._worker(_implement)
        result = self._runner(worker).start(
            plan, issue_authorization(plan), run_id="secret-output-run"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker.invocations))

    def test_prompt_secret_blocks_remote_call(self) -> None:
        task = replace(
            worker_task(),
            objective="fix api_key=FAKE_AUDIT_SECRET_NOT_A_REAL_KEY please",
        )
        plan = worker_plan(task)
        worker = self._worker(_implement)
        result = self._runner(worker).start(
            plan, issue_authorization(plan), run_id="secret-prompt-run"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker.invocations))

    def test_safe_material_completes(self) -> None:
        digest = hashlib.sha256(b"safe seed\n").hexdigest()
        (self.root / "input.txt").write_text("safe seed\n", encoding="utf-8")
        task = worker_task(input_artifacts=(InputArtifact("input.txt", digest),))
        plan = worker_plan(task)
        worker = self._worker(_implement)
        result = self._runner(worker).start(
            plan, issue_authorization(plan), run_id="safe-run"
        )
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(TaskState.APPROVED, self.database.task_state("safe-run", task.task_id))

    def test_local_to_remote_fallback_uses_minimal_sandbox(self) -> None:
        task = replace(
            make_task("one"),
            data_sensitivity=DataSensitivity.PUBLIC,
            allow_remote_implementation=True,
            max_remote_cost=1,
            fallback_model=ModelRef(WORKER_PROVIDER, WORKER_MODEL, "2026-09", "other", False),
        )
        plan = replace(make_plan(tasks=(task,)), budget_mode=BudgetMode.FIXED, max_remote_cost=10)
        remote_calls: list[InvocationRequest] = []

        def responder(request: InvocationRequest) -> InvocationResult:
            if request.model.model_id == "coder":
                raise ModelUnavailableError("fixture preflight unavailable")
            if request.model.is_local:
                return _approve(request)
            remote_calls.append(request)
            return _implement(request)

        adapter = FakeAdapter(responder=responder)
        runner = Runner(
            self.database,
            AdapterRouter({"fake": adapter, WORKER_PROVIDER: adapter}),
            self.workspace,
        )
        result = runner.start(
            plan, issue_authorization(plan), run_id="fallback-sandbox-run"
        )
        self.assertEqual(RunState.COMPLETED, result.state)
        self.assertEqual(1, len(remote_calls))
        sandbox = Path(remote_calls[0].metadata["worktree"])
        self.assertFalse((sandbox / ".git").exists())
        self.assertFalse((sandbox / "seed.txt").exists())
        self.assertTrue((sandbox / ".agentflow-briefing.md").is_file())

    def test_d3_data_never_sent_remotely(self) -> None:
        task = replace(worker_task(), data_sensitivity=DataSensitivity.STRICTLY_PRIVATE)
        plan = worker_plan(task)
        worker = self._worker(_implement)
        result = self._runner(worker).start(
            plan, issue_authorization(plan), run_id="d3-run"
        )
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker.invocations))

    def test_d1_requires_authorization(self) -> None:
        task = replace(worker_task(), data_sensitivity=DataSensitivity.PROJECT_INTERNAL)
        plan = worker_plan(task)
        worker = self._worker(_implement)
        denied = self._runner(worker).start(
            plan, issue_authorization(plan), run_id="d1-denied-run"
        )
        self.assertEqual(RunState.PAUSED, denied.state)
        self.assertEqual(0, len(worker.invocations))

    def test_d1_allowed_with_authorization(self) -> None:
        task = replace(worker_task(), data_sensitivity=DataSensitivity.PROJECT_INTERNAL)
        plan = worker_plan(task)
        allowed_worker = self._worker(_implement)
        allowed = self._runner(allowed_worker).start(
            plan,
            issue_authorization(plan, allow_d1_remote=True),
            run_id="d1-allowed-run",
        )
        self.assertEqual(RunState.COMPLETED, allowed.state)
        self.assertEqual(1, len(allowed_worker.invocations))

    def test_d2_never_sent_without_real_redaction(self) -> None:
        task = replace(worker_task(), data_sensitivity=DataSensitivity.SENSITIVE_INTERNAL)
        plan = worker_plan(task)
        worker = self._worker(_implement)
        result = self._runner(worker).start(
            plan,
            issue_authorization(plan, allow_d2_remote=True),
            run_id="d2-run",
        )
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(0, len(worker.invocations))


if __name__ == "__main__":
    unittest.main()
