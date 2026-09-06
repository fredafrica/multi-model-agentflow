from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentflow.authorization import issue_authorization
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.runner import Runner
from agentflow.states import RunState
from agentflow.workspace import GitWorkspace

from test_runner import git, make_plan, make_task


class PauseAtomicityTests(unittest.TestCase):
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

    def _paused_runner(self, run_id: str) -> Runner:
        task = make_task("one")
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        self.database.save_plan(plan)
        self.database.save_authorization(authorization)
        self.database.create_run(run_id, plan, authorization)
        return Runner(self.database, FakeAdapter(), self.workspace)

    def test_pause_crash_before_wake_records_notification_on_retry(self) -> None:
        runner = self._paused_runner("pause-crash")
        with mock.patch.object(
            self.database,
            "record_supervisor_checkpoint",
            side_effect=KeyboardInterrupt("fixture crash before wake"),
        ):
            with self.assertRaises(KeyboardInterrupt):
                runner._complete_safe_pause(
                    "pause-crash", "one", "unknown_model_call"
                )
        runner._complete_safe_pause("pause-crash", "one", "unknown_model_call")
        pending = self.database.pending_supervisor_checkpoints("pause-crash")
        self.assertEqual(1, len(pending))
        self.assertEqual("unknown_call", pending[0]["reason"])
        self.assertEqual(
            RunState.PAUSED.value,
            self.database.run_snapshot("pause-crash")["run"]["run_state"],
        )

    def test_pause_reentry_is_idempotent(self) -> None:
        runner = self._paused_runner("pause-reentry")
        runner._complete_safe_pause("pause-reentry", "one", "unknown_model_call")
        runner._complete_safe_pause("pause-reentry", "one", "unknown_model_call")
        pending = self.database.pending_supervisor_checkpoints("pause-reentry")
        self.assertEqual(1, len(pending))

    def test_new_event_during_pause_records_new_notification(self) -> None:
        runner = self._paused_runner("pause-new-event")
        runner._complete_safe_pause("pause-new-event", "one", "unknown_model_call")
        runner._complete_safe_pause("pause-new-event", "one", "reviewer_unavailable")
        reasons = [
            item["reason"]
            for item in self.database.pending_supervisor_checkpoints("pause-new-event")
        ]
        self.assertEqual(["unknown_call", "reviewer_unavailable"], reasons)


if __name__ == "__main__":
    unittest.main()
