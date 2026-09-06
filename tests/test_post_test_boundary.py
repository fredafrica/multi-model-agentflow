"""Post-test / post-review artifact boundary regression tests."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentflow.authorization import issue_authorization
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.runner import Runner
from agentflow.states import RunState, TaskState
from agentflow.workspace import GitWorkspace

from test_runner import approved_response, make_plan, make_task


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


class PostTestBoundaryTests(unittest.TestCase):
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

    def test_test_creating_out_of_scope_file_fails_before_approval(self) -> None:
        task = replace(
            make_task("one"),
            test_command=("/bin/sh", "-c", "echo unintended > outside.txt"),
        )
        plan = make_plan(tasks=(task,))
        adapter = FakeAdapter(responder=approved_response)
        result = Runner(self.database, adapter, self.workspace).start(
            plan, issue_authorization(plan), run_id="post-test"
        )
        self.assertEqual(RunState.FAILED, result.state)
        self.assertEqual(
            TaskState.FAILED, self.database.task_state("post-test", task.task_id)
        )
        review_calls = [
            request for request in adapter.invocations if request.role == "review"
        ]
        self.assertEqual([], review_calls)


if __name__ == "__main__":
    unittest.main()
