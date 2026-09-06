"""Regression tests for Group K: read-only observation vs. schema migration."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from agentflow.contracts import (
    BusinessImportance,
    BudgetMode,
    DataSensitivity,
    ModelRef,
    OperationalSafety,
    PlanContract,
    RiskLevel,
    RunMode,
    TaskContract,
)
from agentflow.database import Database
from agentflow.schema import DDL, SCHEMA_VERSION


def _plan() -> PlanContract:
    task = TaskContract(
        task_id="t",
        objective="o",
        risk_level=RiskLevel(
            BusinessImportance.NORMAL, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE
        ),
        allowed_files=("a.txt",),
        forbidden_actions=(),
        acceptance_criteria=("a",),
        data_sensitivity=DataSensitivity.PUBLIC,
        implementation_model=ModelRef("l", "c", "1", "c", True),
        review_model=ModelRef("l", "r", "1", "r", True),
        fallback_model=None,
        max_remote_cost=0,
        max_retry_count=1,
        escalation_conditions=(),
        expected_outputs=("a.txt",),
    )
    return PlanContract(
        plan_id="p",
        schema_version=1,
        version=1,
        run_mode=RunMode.MANAGED,
        budget_mode=BudgetMode.LOCAL_FREE,
        max_remote_cost=0,
        emergency_reserve=0,
        privacy_policy_version="1",
        tasks=(task,),
    )


class ReadOnlyObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agentflow.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _initialize(self) -> Database:
        database = Database(self.path)
        database.initialize()
        return database

    def test_open_readonly_rejects_missing_database(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not initialized"):
            Database.open_readonly(self.path)

    def test_open_readonly_rejects_outdated_schema(self) -> None:
        database = self._initialize()
        database.connection.execute("DELETE FROM schema_meta")
        database.connection.execute(
            "INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)",
            (SCHEMA_VERSION - 1, "now"),
        )
        database.close()
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            Database.open_readonly(self.path)

    def test_open_readonly_reads_committed_snapshot_without_writing(self) -> None:
        database = self._initialize()
        plan = _plan()
        from agentflow.authorization import issue_authorization

        authorization = issue_authorization(plan)
        database.save_plan(plan)
        database.save_authorization(authorization)
        database.create_run("obs-run", plan, authorization)
        before = database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
        database.close()

        readonly = Database.open_readonly(self.path)
        try:
            snapshot = readonly.run_snapshot("obs-run")
            self.assertEqual("running", snapshot["run"]["run_state"])
            with self.assertRaises(sqlite3.OperationalError):
                readonly.connection.execute(
                    "INSERT INTO events(event_id, aggregate_type, aggregate_id, "
                    "event_type, payload_json, created_at) "
                    "VALUES ('x', 'run', 'obs-run', 'x', '{}', 'now')"
                )
        finally:
            readonly.close()

        # Observation must not have changed any business data or events.
        check = Database(self.path)
        try:
            count = check.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
            self.assertEqual(before, count)
        finally:
            check.close()

    def test_initialize_is_idempotent_and_does_not_recreate_indexes(self) -> None:
        database = self._initialize()
        database.close()

        first = Database(self.path)
        try:
            before = dict(
                first.fetch_one(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'idx_model_calls_provider_request'"
                )
            )
            first.initialize()
            after = dict(
                first.fetch_one(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type = 'index' AND name = 'idx_model_calls_provider_request'"
                )
            )
            self.assertEqual(before, after)
        finally:
            first.close()


class LegacySchemaMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_legacy_schema_upgrade_and_reopen_are_idempotent(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(DDL)
            connection.execute("DROP INDEX idx_model_calls_provider_request")
            connection.execute(
                "CREATE UNIQUE INDEX idx_model_calls_provider_request "
                "ON model_calls(provider, provider_request_id) "
                "WHERE provider_request_id IS NOT NULL"
            )
        finally:
            connection.close()

        legacy = Database(legacy_path)
        legacy.initialize()
        legacy.close()

        # A second open/migrate must not fail and must keep the rebuilt index.
        reopen = Database(legacy_path)
        try:
            columns = [
                row["name"]
                for row in reopen.connection.execute(
                    "PRAGMA index_info(idx_model_calls_provider_request)"
                )
            ]
            self.assertIn("segment_index", columns)
            reopen.initialize()
            self.assertIn("segment_index", columns)
        finally:
            reopen.close()


if __name__ == "__main__":
    unittest.main()
