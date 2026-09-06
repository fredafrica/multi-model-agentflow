"""File sync, path validation, and atomic-write regression tests."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agentflow.workspace as ws

from agentflow.contracts import (
    BusinessImportance,
    DataSensitivity,
    InputArtifact,
    ModelRef,
    OperationalSafety,
    RiskLevel,
    TaskContract,
)
from agentflow.workspace import (
    GitWorkspace,
    StagingSyncError,
    _atomic_write_bytes,
    _safe_component,
)


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def make_task(
    task_id: str = "task-1",
    *,
    allowed_files: tuple[str, ...] = ("out.txt",),
    input_artifacts: tuple[InputArtifact, ...] = (),
) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        objective="Make a bounded change",
        risk_level=RiskLevel(
            BusinessImportance.NORMAL, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE
        ),
        allowed_files=allowed_files,
        forbidden_actions=("network",),
        acceptance_criteria=("tests pass",),
        data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
        implementation_model=ModelRef("fake", "coder", "1", "coder-family", True),
        review_model=ModelRef("fake", "reviewer", "1", "reviewer-family", True),
        fallback_model=None,
        max_remote_cost=0,
        max_retry_count=1,
        escalation_conditions=("test failure",),
        expected_outputs=allowed_files,
        input_artifacts=input_artifacts,
    )


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_atomic_write_replaces_destination_symlink_not_target(self) -> None:
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        destination = self.root / "out.txt"
        destination.symlink_to(victim)
        _atomic_write_bytes(destination, b"new")
        self.assertEqual(b"keep", victim.read_bytes())
        self.assertFalse(destination.is_symlink())
        self.assertEqual(b"new", destination.read_bytes())

    def test_atomic_write_ignores_predictable_temp_symlink(self) -> None:
        victim = self.root / "victim"
        victim.write_bytes(b"keep")
        destination = self.root / "out.txt"
        predictable = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
        predictable.symlink_to(victim)
        _atomic_write_bytes(destination, b"new")
        self.assertEqual(b"keep", victim.read_bytes())
        self.assertEqual(b"new", destination.read_bytes())
        self.assertFalse(destination.is_symlink())

    def test_atomic_write_preserves_existing_permissions(self) -> None:
        destination = self.root / "out.txt"
        destination.write_bytes(b"old")
        os.chmod(destination, 0o640)
        _atomic_write_bytes(destination, b"new")
        self.assertEqual(0o640, os.stat(destination).st_mode & 0o777)

    def test_safe_component_does_not_collide_across_distinct_identifiers(self) -> None:
        self.assertNotEqual(_safe_component("a b"), _safe_component("a-b"))
        self.assertNotEqual(_safe_component("a/b"), _safe_component("a-b"))
        self.assertNotEqual(_safe_component("a  b"), _safe_component("a b"))
        self.assertEqual(_safe_component("plain"), "plain")


class SyncRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        self.runs = self.root / ".agentflow" / "runs"
        self.workspace = GitWorkspace(self.root, self.runs)
        self.worktree = self.workspace.create("sync-run", "sync-task")
        self.artifact = InputArtifact(
            "seed.txt", hashlib.sha256(b"seed\n").hexdigest()
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_sandbox(self, allowed_files: tuple[str, ...]) -> Path:
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="sync-run",
            task_id="sync-task",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(self.artifact,),
            allowed_files=allowed_files,
            briefing="briefing",
        )
        return sandbox, baseline

    def test_batch_failure_rolls_back_committed_writes(self) -> None:
        (self.worktree / "out1.txt").write_text("old1\n", encoding="utf-8")
        (self.worktree / "out2.txt").write_text("old2\n", encoding="utf-8")
        sandbox, baseline = self._make_sandbox(("out1.txt", "out2.txt"))
        (sandbox / "out1.txt").write_text("new1\n", encoding="utf-8")
        (sandbox / "out2.txt").write_text("new2\n", encoding="utf-8")
        real_replace = os.replace
        calls = {"n": 0}

        def flaky(source: str, destination: str):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("injected replace failure")
            return real_replace(source, destination)

        with mock.patch.object(ws.os, "replace", side_effect=flaky):
            with self.assertRaises(StagingSyncError) as ctx:
                self.workspace.sync_staging_outputs(
                    sandbox,
                    self.worktree,
                    input_artifacts=(self.artifact,),
                    allowed_files=("out1.txt", "out2.txt"),
                    baseline=baseline,
                )
        self.assertEqual((), ctx.exception.unrecovered_paths)
        self.assertEqual("old1\n", (self.worktree / "out1.txt").read_text())
        self.assertEqual("old2\n", (self.worktree / "out2.txt").read_text())

    def test_rollback_failure_reports_unrecovered_paths(self) -> None:
        (self.worktree / "out1.txt").write_text("old1\n", encoding="utf-8")
        (self.worktree / "out2.txt").write_text("old2\n", encoding="utf-8")
        sandbox, baseline = self._make_sandbox(("out1.txt", "out2.txt"))
        (sandbox / "out1.txt").write_text("new1\n", encoding="utf-8")
        (sandbox / "out2.txt").write_text("new2\n", encoding="utf-8")
        real_replace = os.replace
        calls = {"n": 0}

        def failing_replace(source: str, destination: str):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("always fails during rollback")
            return real_replace(source, destination)

        with mock.patch.object(ws.os, "replace", side_effect=failing_replace):
            with self.assertRaises(StagingSyncError) as ctx:
                self.workspace.sync_staging_outputs(
                    sandbox,
                    self.worktree,
                    input_artifacts=(self.artifact,),
                    allowed_files=("out1.txt", "out2.txt"),
                    baseline=baseline,
                )
        self.assertTrue(ctx.exception.unrecovered_paths)


class BusinessTaskMdTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / "TASK.md").write_text("business task\n", encoding="utf-8")
        git(self.root, "add", "TASK.md")
        git(self.root, "commit", "-m", "seed")
        self.runs = self.root / ".agentflow" / "runs"
        self.workspace = GitWorkspace(self.root, self.runs)
        self.worktree = self.workspace.create("taskmd-run", "taskmd-task")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_business_task_md_is_not_treated_as_control_file(self) -> None:
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="taskmd-run",
            task_id="taskmd-task",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(),
            allowed_files=("TASK.md",),
            briefing="generated briefing",
        )
        self.assertEqual("business task\n", (sandbox / "TASK.md").read_text())
        self.assertTrue((sandbox / ".agentflow-briefing.md").is_file())
        (sandbox / "TASK.md").write_text("worker change\n", encoding="utf-8")
        synced = self.workspace.sync_staging_outputs(
            sandbox,
            self.worktree,
            input_artifacts=(),
            allowed_files=("TASK.md",),
            baseline=baseline,
        )
        self.assertEqual(("TASK.md",), synced)
        self.assertEqual("worker change\n", (self.worktree / "TASK.md").read_text())


class PathNormalizationTests(unittest.TestCase):
    def test_contract_rejects_normalized_overlap(self) -> None:
        artifact = InputArtifact("seed.txt", hashlib.sha256(b"seed\n").hexdigest())
        with self.assertRaises(ValueError):
            make_task(
                allowed_files=("./seed.txt",),
                input_artifacts=(artifact,),
            )

    def test_contract_normalizes_expected_outputs(self) -> None:
        task = TaskContract(
            task_id="task-1",
            objective="Make a bounded change",
            risk_level=RiskLevel(
                BusinessImportance.NORMAL, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE
            ),
            allowed_files=("./out.txt",),
            forbidden_actions=("network",),
            acceptance_criteria=("tests pass",),
            data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
            implementation_model=ModelRef("fake", "coder", "1", "coder-family", True),
            review_model=ModelRef("fake", "reviewer", "1", "reviewer-family", True),
            fallback_model=None,
            max_remote_cost=0,
            max_retry_count=1,
            escalation_conditions=("test failure",),
            expected_outputs=("out.txt",),
        )
        self.assertEqual(("./out.txt",), task.allowed_files)


if __name__ == "__main__":
    unittest.main()
