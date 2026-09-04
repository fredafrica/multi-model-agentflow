from __future__ import annotations

import sqlite3
import unittest
from dataclasses import replace

from agentflow.contracts import (
    BusinessImportance,
    DataSensitivity,
    ModelRef,
    OperationalSafety,
    PlanContract,
    ReviewFinding,
    ReviewResult,
    RiskLevel,
    RunMode,
    BudgetMode,
    Severity,
    TaskContract,
)
from agentflow.schema import DDL
from agentflow.states import ControlState, InvocationState, TaskState, require_transition


def task(task_id: str = "task-1") -> TaskContract:
    model = ModelRef("test", "local", "1", is_local=True)
    return TaskContract(
        task_id=task_id,
        objective="Make a bounded change",
        risk_level=RiskLevel(BusinessImportance.NORMAL, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE),
        allowed_files=("src/example.py",),
        forbidden_actions=("network",),
        acceptance_criteria=("tests pass",),
        data_sensitivity=DataSensitivity.PROJECT_INTERNAL,
        implementation_model=model,
        review_model=ModelRef("other", "reviewer", "1", is_local=True),
        fallback_model=None,
        max_remote_cost=0,
        max_retry_count=1,
        escalation_conditions=("test failure",),
        expected_outputs=("src/example.py",),
    )


class ContractTests(unittest.TestCase):
    def test_task_paths_are_project_relative(self) -> None:
        with self.assertRaises(ValueError):
            task().__class__(**{**task().__dict__, "allowed_files": ("../secret",)})

    def test_plan_rejects_duplicate_task_ids(self) -> None:
        with self.assertRaises(ValueError):
            PlanContract(
                plan_id="plan",
                schema_version=1,
                version=1,
                run_mode=RunMode.MANAGED,
                budget_mode=BudgetMode.LOCAL_FREE,
                max_remote_cost=0,
                emergency_reserve=0,
                privacy_policy_version="1",
                tasks=(task(), task()),
            )

    def test_plan_rejects_forward_or_cyclic_dependencies(self) -> None:
        first = replace(task("first"), depends_on=("later",))
        later = replace(task("later"), depends_on=("first",))
        with self.assertRaises(ValueError):
            PlanContract(
                plan_id="plan",
                schema_version=1,
                version=1,
                run_mode=RunMode.MANAGED,
                budget_mode=BudgetMode.LOCAL_FREE,
                max_remote_cost=0,
                emergency_reserve=0,
                privacy_policy_version="1",
                tasks=(first, later),
            )

    def test_mvp_plan_requires_one_to_four_tasks(self) -> None:
        values = dict(
            plan_id="plan",
            schema_version=1,
            version=1,
            run_mode=RunMode.MANAGED,
            budget_mode=BudgetMode.LOCAL_FREE,
            max_remote_cost=0,
            emergency_reserve=0,
            privacy_policy_version="1",
        )
        with self.assertRaises(ValueError):
            PlanContract(**values, tasks=())
        with self.assertRaises(ValueError):
            PlanContract(
                **values, tasks=tuple(task(f"task-{number}") for number in range(5))
            )

    def test_p0_or_p1_prevents_approval(self) -> None:
        with self.assertRaises(ValueError):
            ReviewResult(
                review_id="review",
                task_id="task-1",
                reviewer=ModelRef("test", "review", "1", is_local=True),
                findings=(ReviewFinding(Severity.P1, "failure", "evidence", True),),
                approved=True,
            )


class StateTests(unittest.TestCase):
    def test_valid_task_transition(self) -> None:
        require_transition(TaskState.RUNNING, TaskState.SELF_TESTING)

    def test_invalid_task_transition(self) -> None:
        with self.assertRaises(ValueError):
            require_transition(TaskState.DRAFT, TaskState.APPROVED)

    def test_state_machines_are_orthogonal(self) -> None:
        with self.assertRaises(ValueError):
            require_transition(TaskState.RUNNING, ControlState.PAUSE_REQUESTED)

    def test_unknown_invocation_can_be_reconciled_but_not_restarted(self) -> None:
        require_transition(InvocationState.UNKNOWN, InvocationState.COMPLETED)
        with self.assertRaises(ValueError):
            require_transition(InvocationState.UNKNOWN, InvocationState.STARTED)


class SchemaTests(unittest.TestCase):
    def test_schema_builds_with_foreign_keys(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(DDL)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertTrue(
            {
                "plans",
                "authorizations",
                "runs",
                "tasks",
                "attempts",
                "model_calls",
                "test_results",
                "reviews",
                "cost_entries",
                "events",
            }.issubset(tables)
        )


if __name__ == "__main__":
    unittest.main()
