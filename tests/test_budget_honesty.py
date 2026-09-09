"""Regression tests for Group J: honest, finite budget and authorization fields."""

from __future__ import annotations

import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from agentflow.authorization import issue_authorization
from agentflow.contracts import (
    AUTHORIZATION_TTL_SECONDS_LIMIT,
    BusinessImportance,
    BudgetMode,
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
from agentflow.policies import invocation_decision
from agentflow.states import InvocationState, TaskState
from resource_budget_fixtures import budgeted_plan_contract


def _remote_task(task_id: str = "task-1") -> TaskContract:
    return TaskContract(
        task_id=task_id,
        objective="implement remotely",
        risk_level=RiskLevel(
            BusinessImportance.NORMAL, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE
        ),
        allowed_files=("out.txt",),
        forbidden_actions=("network",),
        acceptance_criteria=("out.txt exists",),
        data_sensitivity=DataSensitivity.PUBLIC,
        implementation_model=ModelRef("remote", "worker", "1", "worker", False),
        review_model=ModelRef("local", "reviewer", "1", "reviewer", True),
        fallback_model=None,
        max_remote_cost=1,
        max_retry_count=1,
        escalation_conditions=("test failure",),
        expected_outputs=("out.txt",),
        allow_remote_implementation=True,
    )


def _remote_plan(task: TaskContract | None = None) -> PlanContract:
    return budgeted_plan_contract(
        plan_id="budget-plan",
        schema_version=1,
        version=1,
        run_mode=RunMode.MANAGED,
        budget_mode=BudgetMode.FIXED,
        max_remote_cost=1,
        emergency_reserve=0,
        privacy_policy_version="1",
        tasks=(task or _remote_task(),),
    )


class MoneyValidationTests(unittest.TestCase):
    def test_max_remote_cost_rejects_non_finite_and_wrong_types(self) -> None:
        for bad in (float("nan"), float("inf"), -1.0, -0.01, True, "10"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    TaskContract(
                        task_id="t",
                        objective="o",
                        risk_level=RiskLevel(
                            BusinessImportance.NORMAL,
                            OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE,
                        ),
                        allowed_files=("a.txt",),
                        forbidden_actions=(),
                        acceptance_criteria=("a",),
                        data_sensitivity=DataSensitivity.PUBLIC,
                        implementation_model=ModelRef("l", "c", "1", "c", True),
                        review_model=ModelRef("l", "r", "1", "r", True),
                        fallback_model=None,
                        max_remote_cost=bad,
                        max_retry_count=1,
                        escalation_conditions=(),
                        expected_outputs=("a.txt",),
                    )

    def test_plan_budget_and_reserve_reject_non_finite(self) -> None:
        for field in ("max_remote_cost", "emergency_reserve"):
            for bad in (float("nan"), float("inf"), -1.0, True, "10"):
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ValueError):
                        replace(_remote_plan(), **{field: bad})

    def test_invocation_result_rejects_non_finite_reported_cost(self) -> None:
        for bad in (float("nan"), float("inf"), -0.01):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    InvocationResult("p", "out", 1, 1, 0, 1, bad)

    def test_authorization_ttl_cannot_exceed_24_hours(self) -> None:
        with self.assertRaises(ValueError):
            replace(_remote_plan(), authorization_ttl_seconds=172800)
        plan = replace(_remote_plan(), authorization_ttl_seconds=AUTHORIZATION_TTL_SECONDS_LIMIT)
        self.assertEqual(AUTHORIZATION_TTL_SECONDS_LIMIT, plan.authorization_ttl_seconds)
        for bad in (True, 86400.0, "86400"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    replace(_remote_plan(), authorization_ttl_seconds=bad)

    def test_issue_authorization_respects_ttl_cap(self) -> None:
        plan = replace(_remote_plan(), authorization_ttl_seconds=3600)
        auth = issue_authorization(plan)
        self.assertEqual(
            3600,
            (auth.expires_at - auth.authorized_at).total_seconds(),
        )


class BudgetAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agentflow.db"
        self.database = Database(self.path)
        self.database.initialize()

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _setup_run(self, database: Database, plan: PlanContract, task: TaskContract) -> str:
        authorization = issue_authorization(plan)
        database.save_plan(plan)
        database.save_authorization(authorization)
        database.create_run("budget-run", plan, authorization)
        database.transition_task("budget-run", task.task_id, TaskState.WAITING_AUTHORIZATION)
        database.transition_task("budget-run", task.task_id, TaskState.QUEUED)
        database.transition_task("budget-run", task.task_id, TaskState.RUNNING)
        database.create_attempt("budget-attempt", "budget-run", task.task_id, 1)
        return "budget-run"

    def _request(self, task: TaskContract, suffix: str, estimated: float) -> InvocationRequest:
        return InvocationRequest(
            call_id=f"call-{suffix}",
            request_key=f"request-{suffix}",
            run_id="budget-run",
            task_id=task.task_id,
            role="implementation",
            model=task.implementation_model,
            prompt="implement",
            data_sensitivity=task.data_sensitivity,
            read_only=False,
            metadata={"estimated_remote_cost": estimated},
        )

    def test_remote_budget_committed_counts_started_and_unknown_calls(self) -> None:
        task = _remote_task()
        plan = _remote_plan(task)
        self._setup_run(self.database, plan, task)
        started = self._request(task, "started", 0.4)
        unknown = self._request(task, "unknown", 0.3)
        for request in (started, unknown):
            self.database.register_call(request, "budget-attempt")
            self.database.transition_call(request.call_id, InvocationState.STARTED)
        self.database.transition_call(unknown.call_id, InvocationState.UNKNOWN)
        # Both in-flight/unknown remote calls keep their estimate reserved.
        self.assertAlmostEqual(0.7, self.database.remote_budget_committed("budget-run"))

    def test_interleaved_budget_registration_blocks_second_dispatch(self) -> None:
        task = _remote_task()
        plan = _remote_plan(task)
        self._setup_run(self.database, plan, task)
        second = Database(self.path)
        try:
            first = self._request(task, "first", 0.6)
            other = self._request(task, "second", 0.6)
            _, outcome = self.database.begin_call(
                first, "budget-attempt", estimated_remote_cost=0.6, usable_budget=1.0
            )
            self.assertEqual("STARTED", outcome)
            _, outcome = second.begin_call(
                other, "budget-attempt", estimated_remote_cost=0.6, usable_budget=1.0
            )
            self.assertEqual("BUDGET_BLOCKED", outcome)
        finally:
            second.close()

    def test_actual_cost_over_estimate_blocks_subsequent_call(self) -> None:
        task = _remote_task()
        plan = _remote_plan(task)
        authorization = issue_authorization(plan)
        # A prior provider report pushed committed spend to 0.9; another 0.5
        # estimate would exceed the 1.0 usable budget and must be denied.
        decision = invocation_decision(
            self._request(task, "next", 0.5),
            task=task,
            plan=plan,
            authorization=authorization,
            estimated_remote_cost=0.5,
            remote_cost_spent=0.9,
        )
        self.assertFalse(decision.allowed)
        self.assertTrue(any("budget denied" in reason for reason in decision.reasons))

    def test_budget_blocked_leaves_no_orphan_planned_call(self) -> None:
        task = _remote_task()
        plan = _remote_plan(task)
        self._setup_run(self.database, plan, task)
        first = self._request(task, "first", 0.6)
        second = self._request(task, "second", 0.6)
        _, outcome = self.database.begin_call(
            first, "budget-attempt", estimated_remote_cost=0.6, usable_budget=1.0
        )
        self.assertEqual("STARTED", outcome)
        _, outcome = self.database.begin_call(
            second, "budget-attempt", estimated_remote_cost=0.6, usable_budget=1.0
        )
        self.assertEqual("BUDGET_BLOCKED", outcome)
        row = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM model_calls WHERE request_key = 'request-second'"
        )
        self.assertEqual(0, row["count"])
        self.assertEqual(1, self.database.unfinished_calls("budget-run"))
        self.assertEqual(0.6, self.database.remote_budget_committed("budget-run"))
        events = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM events WHERE aggregate_id = 'call-second'"
        )
        self.assertEqual(0, events["count"])

    def test_reported_zero_cost_is_confirmed_but_missing_cost_is_unknown(self) -> None:
        task = _remote_task()
        plan = _remote_plan(task)
        self._setup_run(self.database, plan, task)
        zero = self._request(task, "zero", 0)
        missing = self._request(task, "missing", 0)
        for request in (zero, missing):
            self.database.register_call(request, "budget-attempt")
            self.database.transition_call(request.call_id, InvocationState.STARTED)
        self.database.complete_call(
            zero.call_id, InvocationResult("p0", "ok", 1, 1, 0, 1, 0.0), "budget-run"
        )
        self.database.complete_call(
            missing.call_id,
            InvocationResult("p1", "ok", 1, 1, 0, 1, None),
            "budget-run",
        )
        summary = self.database.cost_summary("budget-run")
        self.assertEqual(0.0, summary["confirmed_remote_cost_usd"])
        self.assertEqual(1, summary["cost_unavailable_calls"])
        # A reported zero is a real, confirmed entry; a missing cost is not.
        entries = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM cost_entries WHERE run_id = 'budget-run'"
        )
        self.assertEqual(1, entries["count"])


if __name__ == "__main__":
    unittest.main()
