from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agentflow.authorization import issue_authorization
from agentflow.cli import main
from agentflow.database import Database
from agentflow.serialization import canonical_json

from test_remote_worker import git, worker_plan


class SupervisorSentinelProtocolTests(unittest.TestCase):
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
        plan = worker_plan()
        authorization = issue_authorization(plan)
        self.database.save_plan(plan)
        self.database.save_authorization(authorization)
        self.database.create_run("protocol-run", plan, authorization)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _put(self, reason: str, max_checkpoints: int = 1) -> str:
        return self.database.record_supervisor_checkpoint(
            "protocol-run", reason, {}, max_chars=6000, max_checkpoints=max_checkpoints
        )

    def _plan_hash(self) -> str:
        return self.database.supervisor_run_plan_hash("protocol-run")

    def _ack(self, checkpoint_id: str, cursor: int | None = None) -> None:
        self.database.acknowledge_supervisor_checkpoint(
            "protocol-run",
            checkpoint_id,
            {
                "action": "pause",
                "plan_hash": self._plan_hash(),
                "cursor": (
                    cursor
                    if cursor is not None
                    else self.database.latest_event_sequence("protocol-run")
                ),
            },
        )

    def test_refreshed_sentinel_rejects_stale_cursor(self) -> None:
        self._ack(self._put("p0_p1_finding"))
        sentinel_id = self._put("timeout")
        old_cursor = self.database.latest_event_sequence("protocol-run")
        self._put("scope_violation")
        with self.assertRaises(ValueError):
            self._ack(sentinel_id, cursor=old_cursor)

    def test_sentinel_is_reused_not_duplicated(self) -> None:
        self._ack(self._put("p0_p1_finding"))
        for _ in range(5):
            self._ack(self._put("unknown_call"))
        row = self.database.fetch_one(
            "SELECT COUNT(*) AS n FROM supervisor_checkpoints WHERE run_id = ?",
            ("protocol-run",),
        )
        self.assertEqual(2, row["n"])

    def test_sentinel_aggregates_dropped_reasons_and_count(self) -> None:
        self._ack(self._put("p0_p1_finding"))
        self.database.record_supervisor_checkpoint(
            "protocol-run", "timeout", {}, max_chars=6000, max_checkpoints=1
        )
        self.database.record_supervisor_checkpoint(
            "protocol-run", "scope_violation", {}, max_chars=6000, max_checkpoints=1
        )
        sentinels = [
            c
            for c in self.database.pending_supervisor_checkpoints("protocol-run")
            if c["reason"] == "checkpoint_limit_reached"
        ]
        self.assertEqual(1, len(sentinels))
        content = json.loads(sentinels[0]["content_json"])
        self.assertEqual("scope_violation", content["dropped_reason"])
        self.assertIn("timeout", content["dropped_reasons"])
        self.assertIn("scope_violation", content["dropped_reasons"])
        self.assertEqual(2, content["dropped_count"])

    def test_digest_exposes_checkpoint_handle_and_dropped_reason(self) -> None:
        self._ack(self._put("p0_p1_finding"))
        sentinel_id = self._put("timeout")
        digest = self.database.supervisor_digest("protocol-run")
        encoded = canonical_json(digest)
        self.assertIn(sentinel_id, encoded)
        self.assertIn("timeout", encoded)
        self.assertIn("pending_checkpoints", digest)
        handles = [p["checkpoint_id"] for p in digest["pending_checkpoints"]]
        self.assertIn(sentinel_id, handles)

    def test_same_cursor_does_not_wake_again(self) -> None:
        self._put("p0_p1_finding")
        current = self.database.latest_event_sequence("protocol-run")
        stream = io.StringIO()
        with redirect_stdout(stream):
            code = main(
                [
                    "--project",
                    str(self.root),
                    "supervisor-next",
                    "protocol-run",
                    "--after-sequence",
                    str(current),
                    "--wait-seconds",
                    "0",
                ]
            )
        self.assertEqual(0, code)
        out = json.loads(stream.getvalue())
        self.assertFalse(out["changed"])
        self.assertFalse(out["wake_required"])

    def test_invalid_decision_action_rejected(self) -> None:
        checkpoint_id = self._put("p0_p1_finding")
        with self.assertRaises(ValueError):
            self.database.acknowledge_supervisor_checkpoint(
                "protocol-run",
                checkpoint_id,
                {
                    "action": "arbitrary_invalid_action",
                    "plan_hash": self._plan_hash(),
                    "cursor": self.database.latest_event_sequence("protocol-run"),
                },
            )

    def test_unknown_decision_field_rejected(self) -> None:
        checkpoint_id = self._put("p0_p1_finding")
        with self.assertRaises(ValueError):
            self.database.acknowledge_supervisor_checkpoint(
                "protocol-run",
                checkpoint_id,
                {
                    "action": "pause",
                    "plan_hash": self._plan_hash(),
                    "cursor": self.database.latest_event_sequence("protocol-run"),
                    "expand_authorization": True,
                },
            )

    def test_digest_uses_consistent_snapshot(self) -> None:
        self.database.record_supervisor_checkpoint(
            "protocol-run", "p0_p1_finding", {}, max_chars=6000, max_checkpoints=10
        )
        second = Database(self.database.path)
        try:
            original = self.database.pending_supervisor_checkpoints

            def interleave(run: str) -> list[dict]:
                rows = original(run)
                second.record_supervisor_checkpoint(
                    run, "scope_violation", {}, max_chars=6000, max_checkpoints=10
                )
                return rows

            with mock.patch.object(
                self.database,
                "pending_supervisor_checkpoints",
                side_effect=interleave,
            ):
                digest = self.database.supervisor_digest("protocol-run")
            latest = self.database.latest_event_sequence("protocol-run")
            covers_new_event = digest["cursor"] == latest
            reason_visible = "scope_violation" in digest["reason_codes"]
            self.assertEqual(covers_new_event, reason_visible)
        finally:
            second.close()


if __name__ == "__main__":
    unittest.main()
