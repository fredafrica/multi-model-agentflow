from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agentflow.authorization import issue_authorization, validate_authorization
from agentflow.adapters import InvocationOutcomeUnknown
from agentflow.config import resolve_paths
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
from agentflow.database import Database, DuplicateInvocationError, UnknownInvocationError
from agentflow.fake_adapter import FakeAdapter
from agentflow.policies import (
    changed_files_decision,
    invocation_decision,
    remote_data_decision,
    review_independence_decision,
)
from agentflow.serialization import canonical_json, plan_hash
from agentflow.schema import DDL
from agentflow.service import (
    ConfirmationRequiredError,
    InvocationContext,
    InvocationService,
    PolicyDeniedError,
)
from agentflow.states import InvocationState, RunState, TaskState


def make_task(
    task_id: str = "task-1",
    *,
    importance: BusinessImportance = BusinessImportance.NORMAL,
    safety: OperationalSafety = OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE,
    sensitivity: DataSensitivity = DataSensitivity.PROJECT_INTERNAL,
    same_family: bool = False,
) -> TaskContract:
    return TaskContract(
        task_id=task_id,
        objective="Make a bounded change",
        risk_level=RiskLevel(importance, safety),
        allowed_files=(f"src/{task_id}.py",),
        forbidden_actions=("publish",),
        acceptance_criteria=("tests pass",),
        data_sensitivity=sensitivity,
        implementation_model=ModelRef("local", "coder", "1", "family-a", True),
        review_model=ModelRef(
            "local", "reviewer", "1", "family-a" if same_family else "family-b", True
        ),
        fallback_model=None,
        max_remote_cost=1,
        max_retry_count=1,
        escalation_conditions=("test failure",),
        expected_outputs=(f"src/{task_id}.py",),
    )


def make_plan(
    *,
    mode: RunMode = RunMode.MANAGED,
    budget_mode: BudgetMode = BudgetMode.FIXED,
    task: TaskContract | None = None,
) -> PlanContract:
    return PlanContract(
        plan_id="plan-1",
        schema_version=1,
        version=1,
        run_mode=mode,
        budget_mode=budget_mode,
        max_remote_cost=2,
        emergency_reserve=0.5,
        privacy_policy_version="1",
        tasks=(task or make_task(),),
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


class SerializationTests(unittest.TestCase):
    def test_hash_is_stable_and_changes_with_plan(self) -> None:
        plan = make_plan()
        self.assertEqual(plan_hash(plan), plan_hash(plan))
        changed = replace(plan, version=2)
        self.assertNotEqual(plan_hash(plan), plan_hash(changed))
        self.assertNotEqual(
            plan_hash(plan),
            plan_hash(replace(plan, allowed_provider_ids=plan.provider_ids)),
        )
        self.assertNotEqual(
            plan_hash(plan),
            plan_hash(replace(plan, authorization_ttl_seconds=60)),
        )
        self.assertEqual(canonical_json(plan), canonical_json(plan))


class AuthorizationTests(unittest.TestCase):
    def test_authorization_is_plan_bound_and_expires(self) -> None:
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        plan = make_plan()
        authorization = issue_authorization(plan, now=now)
        validate_authorization(authorization, plan, now=now + timedelta(hours=23))
        with self.assertRaises(ValueError):
            validate_authorization(authorization, replace(plan, version=2), now=now)
        with self.assertRaises(ValueError):
            validate_authorization(authorization, plan, now=now + timedelta(hours=24))

    def test_authorization_binds_provider_privacy_policy_and_plan_ttl(self) -> None:
        now = datetime(2026, 9, 4, tzinfo=timezone.utc)
        plan = replace(
            make_plan(),
            allowed_provider_ids=("local",),
            authorization_ttl_seconds=60,
        )
        authorization = issue_authorization(plan, now=now)
        self.assertEqual(("local",), authorization.authorized_provider_ids)
        self.assertEqual(now + timedelta(seconds=60), authorization.expires_at)
        with self.assertRaisesRegex(ValueError, "privacy policy"):
            validate_authorization(
                replace(authorization, privacy_policy_version="changed"),
                plan,
                now=now,
            )


class ConfigTests(unittest.TestCase):
    def test_path_roots_can_be_overridden(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(
                "os.environ",
                {
                    "AGENTFLOW_CONFIG_HOME": f"{directory}/config",
                    "AGENTFLOW_DATA_HOME": f"{directory}/data",
                },
            ):
                paths = resolve_paths(directory)
                self.assertEqual(Path(directory) / "config" / "agentflow", paths.user_config)
                self.assertEqual(Path(directory) / "data" / "agentflow", paths.user_data)


class PolicyTests(unittest.TestCase):
    def test_remote_data_rules_cover_d0_through_d3(self) -> None:
        plan = make_plan()
        denied = issue_authorization(plan)
        allowed = issue_authorization(
            plan, allow_d1_remote=True, allow_d2_remote=True
        )
        decisions = (
            remote_data_decision(
                DataSensitivity.PUBLIC,
                authorization=denied,
                redaction_passed=False,
                content="public excerpt",
            ),
            remote_data_decision(
                DataSensitivity.PROJECT_INTERNAL,
                authorization=denied,
                redaction_passed=True,
                content="project excerpt",
            ),
            remote_data_decision(
                DataSensitivity.PROJECT_INTERNAL,
                authorization=allowed,
                redaction_passed=True,
                content="project excerpt",
            ),
            remote_data_decision(
                DataSensitivity.STRICTLY_PRIVATE,
                authorization=allowed,
                redaction_passed=True,
                content="private excerpt",
            ),
        )
        self.assertEqual([True, False, True, False], [item.allowed for item in decisions])

    def test_d2_requires_authorization_and_redaction(self) -> None:
        plan = make_plan(task=make_task(sensitivity=DataSensitivity.SENSITIVE_INTERNAL))
        denied = issue_authorization(plan, allow_d2_remote=False)
        allowed = issue_authorization(plan, allow_d2_remote=True)
        self.assertFalse(
            remote_data_decision(
                DataSensitivity.SENSITIVE_INTERNAL,
                authorization=denied,
                redaction_passed=True,
                content="safe",
            ).allowed
        )
        self.assertFalse(
            remote_data_decision(
                DataSensitivity.SENSITIVE_INTERNAL,
                authorization=allowed,
                redaction_passed=False,
                content="safe",
            ).allowed
        )
        self.assertTrue(
            remote_data_decision(
                DataSensitivity.SENSITIVE_INTERNAL,
                authorization=allowed,
                redaction_passed=True,
                content="safe",
            ).allowed
        )

    def test_secret_and_personal_path_are_blocked(self) -> None:
        authorization = issue_authorization(make_plan(), allow_d2_remote=True)
        decision = remote_data_decision(
            DataSensitivity.PUBLIC,
            authorization=authorization,
            redaction_passed=True,
            content="api_key=abc123 /Users/alice/project",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(2, len(decision.reasons))

    def test_json_credentials_generic_tokens_accounts_and_home_paths_are_blocked(self) -> None:
        authorization = issue_authorization(make_plan())
        for content in (
            '{"token":"abc123"}',
            '{"username":"example-user"}',
            "/home/example/project",
        ):
            with self.subTest(content=content):
                self.assertFalse(
                    remote_data_decision(
                        DataSensitivity.PUBLIC,
                        authorization=authorization,
                        redaction_passed=False,
                        content=content,
                    ).allowed
                )

    def test_modes_have_deterministic_confirmation_points(self) -> None:
        for mode, expected in (
            (RunMode.MANAGED, False),
            (RunMode.SUPERVISED, True),
            (RunMode.ADAPTIVE, False),
        ):
            task = make_task()
            plan = make_plan(mode=mode, task=task)
            decision = invocation_decision(
                request_for(task),
                task=task,
                plan=plan,
                authorization=issue_authorization(plan),
                estimated_remote_cost=0,
                remote_cost_spent=0,
            )
            self.assertEqual(expected, decision.requires_confirmation)

    def test_adaptive_requires_confirmation_for_important_task(self) -> None:
        task = make_task(importance=BusinessImportance.IMPORTANT)
        plan = make_plan(mode=RunMode.ADAPTIVE, task=task)
        decision = invocation_decision(
            request_for(task),
            task=task,
            plan=plan,
            authorization=issue_authorization(plan),
            estimated_remote_cost=0,
            remote_cost_spent=0,
        )
        self.assertTrue(decision.requires_confirmation)

    def test_local_free_blocks_remote_model(self) -> None:
        local_task = make_task(sensitivity=DataSensitivity.PUBLIC)
        remote_model = replace(local_task.implementation_model, is_local=False)
        task = replace(local_task, implementation_model=remote_model)
        plan = make_plan(budget_mode=BudgetMode.LOCAL_FREE, task=task)
        decision = invocation_decision(
            request_for(task),
            task=task,
            plan=plan,
            authorization=issue_authorization(plan),
            estimated_remote_cost=0,
            remote_cost_spent=0,
        )
        self.assertFalse(decision.allowed)

    def test_fixed_budget_blocks_call_before_adapter(self) -> None:
        local_task = make_task(sensitivity=DataSensitivity.PUBLIC)
        task = replace(
            local_task,
            implementation_model=replace(local_task.implementation_model, is_local=False),
        )
        plan = replace(
            make_plan(task=task), max_remote_cost=1, emergency_reserve=0.25
        )
        decision = invocation_decision(
            request_for(task),
            task=task,
            plan=plan,
            authorization=issue_authorization(plan),
            estimated_remote_cost=0.5,
            remote_cost_spent=0.5,
        )
        self.assertFalse(decision.allowed)
        self.assertIn(
            "budget denied: remote cost would exceed the usable budget",
            decision.reasons,
        )

    def test_adaptive_critical_task_requires_confirmation(self) -> None:
        task = make_task()
        plan = replace(
            make_plan(mode=RunMode.ADAPTIVE, task=task),
            critical_task_ids=(task.task_id,),
        )
        decision = invocation_decision(
            request_for(task),
            task=task,
            plan=plan,
            authorization=issue_authorization(plan),
            estimated_remote_cost=0,
            remote_cost_spent=0,
        )
        self.assertTrue(decision.requires_confirmation)

    def test_important_review_requires_different_family(self) -> None:
        task = make_task(importance=BusinessImportance.IMPORTANT, same_family=True)
        self.assertFalse(review_independence_decision(task).allowed)

    def test_changed_files_must_be_allowed(self) -> None:
        self.assertTrue(changed_files_decision(("a.py",), ("a.py",)).allowed)
        self.assertFalse(changed_files_decision(("a.py",), ("b.py",)).allowed)


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "agentflow.db")
        self.database.initialize()
        self.task = make_task()
        self.plan = make_plan(task=self.task)
        self.authorization = issue_authorization(self.plan)
        self.database.save_plan(self.plan)
        self.database.save_authorization(self.authorization)
        self.database.create_run("run-1", self.plan, self.authorization)
        self.database.transition_task("run-1", self.task.task_id, TaskState.WAITING_AUTHORIZATION)
        self.database.transition_task("run-1", self.task.task_id, TaskState.QUEUED)
        self.database.transition_task("run-1", self.task.task_id, TaskState.RUNNING)
        self.database.create_attempt("attempt-1", "run-1", self.task.task_id, 1)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_event_and_state_update_are_committed_together(self) -> None:
        before = self.database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
        self.database.transition_task("run-1", self.task.task_id, TaskState.SELF_TESTING)
        row = self.database.fetch_one(
            "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?", ("run-1", self.task.task_id)
        )
        after = self.database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
        self.assertEqual(TaskState.SELF_TESTING.value, row["state"])
        self.assertEqual(before + 1, after)

    def test_transaction_rolls_back_event_and_state(self) -> None:
        before = self.database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
        with self.assertRaises(RuntimeError):
            with self.database.transaction() as connection:
                connection.execute(
                    "UPDATE tasks SET state = ? WHERE run_id = ? AND task_id = ?",
                    (TaskState.FAILED.value, "run-1", self.task.task_id),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        event_id, aggregate_type, aggregate_id, event_type, payload_json, created_at
                    ) VALUES ('forced', 'task', 'forced', 'forced', '{}', 'now')
                    """
                )
                raise RuntimeError("simulated crash")
        row = self.database.fetch_one(
            "SELECT state FROM tasks WHERE run_id = ? AND task_id = ?", ("run-1", self.task.task_id)
        )
        after = self.database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
        self.assertEqual(TaskState.RUNNING.value, row["state"])
        self.assertEqual(before, after)

    def test_unknown_call_cannot_be_registered_again(self) -> None:
        request = request_for(self.task)
        self.database.register_call(request, "attempt-1")
        self.database.transition_call(request.call_id, InvocationState.STARTED)
        self.database.transition_call(request.call_id, InvocationState.UNKNOWN)
        row = self.database.fetch_one(
            "SELECT input_tokens, output_tokens, duration_ms, started_at, finished_at "
            "FROM model_calls WHERE call_id = ?",
            (request.call_id,),
        )
        self.assertEqual((0, 0), (row["input_tokens"], row["output_tokens"]))
        self.assertIsNotNone(row["duration_ms"])
        self.assertIsNotNone(row["started_at"])
        self.assertIsNotNone(row["finished_at"])
        with self.assertRaises(UnknownInvocationError):
            self.database.register_call(request, "attempt-1")

    def test_completed_call_is_reused_without_second_cost_entry(self) -> None:
        request = request_for(self.task)
        self.database.register_call(request, "attempt-1")
        self.database.transition_call(request.call_id, InvocationState.STARTED)
        result = InvocationResult("provider-1", "ok", 1, 2, 3, 4, 0.25)
        self.database.complete_call(request.call_id, result, "run-1")
        existing, created = self.database.register_call(request, "attempt-1")
        costs = self.database.fetch_one("SELECT COUNT(*) AS count FROM cost_entries")
        self.assertFalse(created)
        self.assertEqual(InvocationState.COMPLETED.value, existing["state"])
        self.assertEqual(1, costs["count"])

    def test_duplicate_in_flight_call_is_blocked(self) -> None:
        request = request_for(self.task)
        self.database.register_call(request, "attempt-1")
        with self.assertRaises(DuplicateInvocationError):
            self.database.register_call(request, "attempt-1")

    def test_checkpoint_is_saved_with_an_event(self) -> None:
        before = self.database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
        self.database.save_checkpoint("run-1", {"next_task": "task-1"})
        row = self.database.fetch_one("SELECT checkpoint_json FROM runs WHERE run_id = ?", ("run-1",))
        after = self.database.fetch_one("SELECT COUNT(*) AS count FROM events")["count"]
        self.assertEqual('{"next_task":"task-1"}', row["checkpoint_json"])
        self.assertEqual(before + 1, after)

    def test_only_one_active_run_and_one_use_per_authorization(self) -> None:
        with self.assertRaises(ValueError):
            self.database.create_run("run-2", self.plan, self.authorization)
        self.database.set_run_state("run-1", RunState.COMPLETED)
        with self.assertRaises(ValueError):
            self.database.create_run("run-2", self.plan, self.authorization)

    def test_v1_nonnullable_cost_column_migrates_to_nullable(self) -> None:
        legacy_path = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(
                DDL.replace("remote_cost REAL,", "remote_cost REAL NOT NULL DEFAULT 0,")
            )
        finally:
            connection.close()
        legacy = Database(legacy_path)
        try:
            legacy.initialize()
            columns = {
                row["name"]: row
                for row in legacy.connection.execute("PRAGMA table_info(model_calls)")
            }
            self.assertEqual(0, columns["remote_cost"]["notnull"])
        finally:
            legacy.close()


class InvocationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "agentflow.db")
        self.database.initialize()
        self.task = make_task()
        self.plan = make_plan(task=self.task)
        self.authorization = issue_authorization(self.plan)
        self.database.save_plan(self.plan)
        self.database.save_authorization(self.authorization)
        self.database.create_run("run-1", self.plan, self.authorization)
        self.database.transition_task("run-1", self.task.task_id, TaskState.WAITING_AUTHORIZATION)
        self.database.transition_task("run-1", self.task.task_id, TaskState.QUEUED)
        self.database.transition_task("run-1", self.task.task_id, TaskState.RUNNING)
        self.database.create_attempt("attempt-1", "run-1", self.task.task_id, 1)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_fake_adapter_call_is_recorded_without_cost(self) -> None:
        request = request_for(self.task, "service")
        service = InvocationService(self.database, FakeAdapter())
        result = service.invoke(
            request,
            InvocationContext(
                self.plan, self.task, self.authorization, "attempt-1"
            ),
        )
        self.assertEqual(0, result.remote_cost)
        self.assertEqual(1, len(service.adapter.invocations))

    def test_local_adapter_reported_cost_is_normalized_to_zero(self) -> None:
        request = request_for(self.task, "local-cost-normalized")
        adapter = FakeAdapter(
            responder=lambda _request: InvocationResult(
                "local-provider", "ok", 1, 1, 0, 1, 2.5
            )
        )
        result = InvocationService(self.database, adapter).invoke(
            request,
            InvocationContext(
                self.plan, self.task, self.authorization, "attempt-1"
            ),
        )
        self.assertEqual(0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_supervised_mode_requires_confirmation_before_adapter(self) -> None:
        task = self.task
        plan = replace(self.plan, run_mode=RunMode.SUPERVISED)
        authorization = issue_authorization(plan)
        request = request_for(task, "supervised")
        adapter = FakeAdapter()
        service = InvocationService(self.database, adapter)
        with self.assertRaises(ConfirmationRequiredError):
            service.invoke(
                request,
                InvocationContext(plan, task, authorization, "attempt-1"),
            )
        self.assertEqual([], adapter.invocations)

    def test_unknown_adapter_result_is_not_retried(self) -> None:
        def unknown(_request: InvocationRequest) -> InvocationResult:
            raise InvocationOutcomeUnknown("lost response", "provider-unknown")

        request = request_for(self.task, "unknown-service")
        adapter = FakeAdapter(responder=unknown)
        service = InvocationService(self.database, adapter)
        context = InvocationContext(
            self.plan, self.task, self.authorization, "attempt-1"
        )
        with self.assertRaises(InvocationOutcomeUnknown):
            service.invoke(request, context)
        with self.assertRaises(UnknownInvocationError):
            service.invoke(request, context)
        self.assertEqual(1, len(adapter.invocations))

    def test_unknown_call_can_be_resolved_and_reused_with_output(self) -> None:
        def unknown(_request: InvocationRequest) -> InvocationResult:
            raise InvocationOutcomeUnknown("lost response", "provider-resolved")

        request = request_for(self.task, "resolve-service")
        adapter = FakeAdapter(responder=unknown)
        service = InvocationService(self.database, adapter)
        context = InvocationContext(
            self.plan, self.task, self.authorization, "attempt-1"
        )
        with self.assertRaises(InvocationOutcomeUnknown):
            service.invoke(request, context)
        recovered = InvocationResult(
            "provider-resolved", "recovered output", 3, 4, 5, 6, 0.25
        )
        adapter.results["provider-resolved"] = recovered
        self.assertEqual(recovered, service.resolve_unknown("run-1", request.call_id))
        reused = service.invoke(request, context)
        self.assertEqual("recovered output", reused.output)
        self.assertTrue(reused.raw_metadata["reused"])
        self.assertEqual(1, len(adapter.invocations))

    def test_out_of_scope_model_is_denied_and_audited(self) -> None:
        blocked = ModelRef("remote", "blocked", "1", "blocked", False)
        plan = replace(self.plan, blocked_model_keys=(blocked.registry_key,))
        base_authorization = issue_authorization(plan)
        authorization = replace(
            base_authorization,
            authorized_model_keys=(
                *base_authorization.authorized_model_keys,
                blocked.registry_key,
            ),
        )
        request = replace(
            request_for(self.task, "blocked"),
            model=blocked,
            data_sensitivity=DataSensitivity.PUBLIC,
        )
        adapter = FakeAdapter()
        service = InvocationService(self.database, adapter)
        with self.assertRaises(PolicyDeniedError):
            service.invoke(
                request,
                InvocationContext(plan, self.task, authorization, "attempt-1"),
            )
        events = self.database.event_rows("run-1")
        self.assertEqual([], adapter.invocations)
        self.assertIn("call.denied", [event["event_type"] for event in events])


if __name__ == "__main__":
    unittest.main()
