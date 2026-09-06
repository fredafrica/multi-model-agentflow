"""Regression tests for staging output-target baseline conflict detection (follow-up #5)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from agentflow.contracts import InputArtifact
from agentflow.workspace import GitWorkspace, StagingSyncError


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


class StagingBaselineTests(unittest.TestCase):
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
        self.worktree = self.workspace.create("run-1", "task-1")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _sandbox(self, allowed_files: tuple[str, ...]) -> tuple[Path, dict]:
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="run-1",
            task_id="task-1",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(),
            allowed_files=allowed_files,
            briefing="briefing",
        )
        return sandbox, baseline

    def _write_worker(self, sandbox: Path, relative: str, content: str) -> None:
        path = sandbox / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _sync(self, sandbox: Path, baseline, allowed_files):
        return self.workspace.sync_staging_outputs(
            sandbox,
            self.worktree,
            input_artifacts=(),
            allowed_files=allowed_files,
            baseline=baseline,
        )

    def test_existing_file_externally_modified_is_not_overwritten(self) -> None:
        (self.worktree / "out.txt").write_text("baseline\n", encoding="utf-8")
        sandbox, baseline = self._sandbox(("out.txt",))
        self._write_worker(sandbox, "out.txt", "worker\n")
        (self.worktree / "out.txt").write_text("owner\n", encoding="utf-8")
        with self.assertRaises(StagingSyncError):
            self._sync(sandbox, baseline, ("out.txt",))
        self.assertEqual("owner\n", (self.worktree / "out.txt").read_text())

    def test_originally_absent_path_externally_created_is_not_overwritten(self) -> None:
        sandbox, baseline = self._sandbox(("out.txt",))
        self.assertEqual({"exists": False}, baseline["out.txt"])
        self._write_worker(sandbox, "out.txt", "worker\n")
        (self.worktree / "out.txt").write_text("owner\n", encoding="utf-8")
        with self.assertRaises(StagingSyncError):
            self._sync(sandbox, baseline, ("out.txt",))
        self.assertEqual("owner\n", (self.worktree / "out.txt").read_text())

    def test_deleted_destination_is_detected_as_conflict(self) -> None:
        (self.worktree / "out.txt").write_text("baseline\n", encoding="utf-8")
        sandbox, baseline = self._sandbox(("out.txt",))
        self._write_worker(sandbox, "out.txt", "worker\n")
        (self.worktree / "out.txt").unlink()
        with self.assertRaises(StagingSyncError):
            self._sync(sandbox, baseline, ("out.txt",))
        self.assertFalse((self.worktree / "out.txt").exists())

    def test_type_changed_destination_is_detected_as_conflict(self) -> None:
        (self.worktree / "out.txt").write_text("baseline\n", encoding="utf-8")
        sandbox, baseline = self._sandbox(("out.txt",))
        self._write_worker(sandbox, "out.txt", "worker\n")
        (self.worktree / "out.txt").unlink()
        (self.worktree / "out.txt").mkdir()
        with self.assertRaises(StagingSyncError):
            self._sync(sandbox, baseline, ("out.txt",))
        self.assertTrue((self.worktree / "out.txt").is_dir())

    def test_multi_file_conflict_leaves_all_targets_unchanged(self) -> None:
        (self.worktree / "a.txt").write_text("baseline a\n", encoding="utf-8")
        (self.worktree / "b.txt").write_text("baseline b\n", encoding="utf-8")
        sandbox, baseline = self._sandbox(("a.txt", "b.txt"))
        self._write_worker(sandbox, "a.txt", "worker a\n")
        self._write_worker(sandbox, "b.txt", "worker b\n")
        (self.worktree / "a.txt").write_text("owner a\n", encoding="utf-8")
        with self.assertRaises(StagingSyncError):
            self._sync(sandbox, baseline, ("a.txt", "b.txt"))
        self.assertEqual("owner a\n", (self.worktree / "a.txt").read_text())
        self.assertEqual("baseline b\n", (self.worktree / "b.txt").read_text())

    def test_no_conflict_sync_and_idempotent_resync_succeed(self) -> None:
        (self.worktree / "out.txt").write_text("baseline\n", encoding="utf-8")
        sandbox, baseline = self._sandbox(("out.txt",))
        self._write_worker(sandbox, "out.txt", "worker\n")
        produced = self._sync(sandbox, baseline, ("out.txt",))
        self.assertEqual(("out.txt",), produced)
        self.assertEqual("worker\n", (self.worktree / "out.txt").read_text())
        produced_again = self._sync(sandbox, baseline, ("out.txt",))
        self.assertEqual(("out.txt",), produced_again)
        self.assertEqual("worker\n", (self.worktree / "out.txt").read_text())

    def test_baseline_persisted_round_trips_through_canonical_json(self) -> None:
        (self.worktree / "out.txt").write_text("baseline\n", encoding="utf-8")
        sandbox, baseline = self._sandbox(("out.txt",))
        entry = baseline["out.txt"]
        self.assertTrue(entry["exists"])
        self.assertIn("sha256", entry)
        self.assertIn("mode", entry)


if __name__ == "__main__":
    unittest.main()
