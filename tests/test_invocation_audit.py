"""Regression tests for remote invocation failure cost/usage honesty (Group follow-up #3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agentflow.adapters import InvocationOutcomeUnknown, WorkerProtocolError
from agentflow.authorization import issue_authorization
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
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.service import InvocationContext, InvocationService
from agentflow.states import InvocationState, TaskState


def _remote_task() -> TaskContract:
    return TaskContract(
        task_id="task-1",
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
    return PlanContract(
        plan_id="audit-plan",
        schema_version=1,
        version=1,
        run_mode=RunMode.MANAGED,
        budget_mode=BudgetMode.FIXED,
        max_remote_cost=1,
        emergency_reserve=0,
        privacy_policy_version="1",
        tasks=(task or _remote_task(),),
        allowed_provider_ids=("local", "remote"),
    )


class RemoteUnknownAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "agentflow.db"
        self.database = Database(self.path)
        self.database.initialize()
        self.task = _remote_task()
        self.plan = _remote_plan(self.task)
        self.authorization = issue_authorization(self.plan)
        self.database.save_plan(self.plan)
        self.database.save_authorization(self.authorization)
        self.database.create_run("run-1", self.plan, self.authorization)
        self.database.transition_task(
            "run-1", self.task.task_id, TaskState.WAITING_AUTHORIZATION
        )
        self.database.transition_task("run-1", self.task.task_id, TaskState.QUEUED)
        self.database.transition_task("run-1", self.task.task_id, TaskState.RUNNING)
        self.database.create_attempt("attempt-1", "run-1", self.task.task_id, 1)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _request(self, key: str, estimated: float) -> InvocationRequest:
        return InvocationRequest(
            call_id=f"call-{key}",
            request_key=f"request-{key}",
            run_id="run-1",
            task_id=self.task.task_id,
            role="implementation",
            model=self.task.implementation_model,
            prompt="implement",
            data_sensitivity=self.task.data_sensitivity,
            read_only=False,
            metadata={"estimated_remote_cost": estimated},
        )

    def _context(self, estimated: float) -> InvocationContext:
        return InvocationContext(
            self.plan,
            self.task,
            self.authorization,
            "attempt-1",
            estimated_remote_cost=estimated,
        )

    def _row(self, call_id: str):
        return self.database.fetch_one(
            "SELECT * FROM model_calls WHERE call_id = ?", (call_id,)
        )

    def test_no_result_remote_unknown_marks_cost_unavailable_and_reason(self) -> None:
        def unknown(_request: InvocationRequest) -> InvocationResult:
            raise InvocationOutcomeUnknown(
                "remote worker terminated", "provider-ref",
                termination_reason="signal_terminated",
            )

        request = self._request("no-result", 0.5)
        adapter = FakeAdapter(responder=unknown)
        service = InvocationService(self.database, adapter)
        with self.assertRaises(InvocationOutcomeUnknown) as raised:
            service.invoke(request, self._context(0.5))
        self.assertEqual("signal_terminated", raised.exception.termination_reason)

        row = self._row(request.call_id)
        self.assertEqual(InvocationState.UNKNOWN.value, row["state"])
        self.assertEqual(1, row["cost_unavailable"])
        self.assertIsNone(row["remote_cost"])
        metadata = json.loads(row["raw_metadata_json"])
        self.assertEqual("signal_terminated", metadata["termination_reason"])
        summary = self.database.cost_summary("run-1")
        self.assertEqual(0.0, summary["confirmed_remote_cost_usd"])
        self.assertEqual(0.5, summary["reserved_remote_cost_usd"])
        self.assertEqual(1, summary["cost_unavailable_calls"])
        self.assertEqual(1, len(adapter.invocations))

    def test_worker_protocol_error_preserves_confirmed_cost(self) -> None:
        partial = InvocationResult(
            provider_request_id="provider-ref",
            output="",
            input_tokens=17,
            output_tokens=9,
            first_token_latency_ms=None,
            duration_ms=3,
            remote_cost=0.125,
            raw_metadata={"termination_reason": "nonzero_exit"},
        )

        def protocol_failure(_request: InvocationRequest) -> InvocationResult:
            raise WorkerProtocolError("remote worker failed", result=partial)

        request = self._request("protocol", 0.5)
        adapter = FakeAdapter(responder=protocol_failure)
        service = InvocationService(self.database, adapter)
        with self.assertRaises(WorkerProtocolError):
            service.invoke(request, self._context(0.5))

        row = self._row(request.call_id)
        self.assertEqual(InvocationState.FAILED.value, row["state"])
        self.assertEqual(0.125, row["remote_cost"])
        self.assertEqual(0, row["cost_unavailable"])
        self.assertEqual((17, 9), (row["input_tokens"], row["output_tokens"]))
        summary = self.database.cost_summary("run-1")
        self.assertEqual(0.125, summary["confirmed_remote_cost_usd"])
        self.assertEqual(1, len(adapter.invocations))
        entries = self.database.fetch_one(
            "SELECT COUNT(*) AS count FROM cost_entries WHERE run_id = 'run-1'"
        )
        self.assertEqual(1, entries["count"])

    def test_worker_protocol_error_without_cost_evidence_is_unavailable(self) -> None:
        partial = InvocationResult(
            provider_request_id="provider-ref",
            output="",
            input_tokens=5,
            output_tokens=3,
            first_token_latency_ms=None,
            duration_ms=3,
            remote_cost=None,
            raw_metadata={"termination_reason": "no_usable_result"},
        )

        def protocol_failure(_request: InvocationRequest) -> InvocationResult:
            raise WorkerProtocolError("no usable result", result=partial)

        request = self._request("no-cost", 0.5)
        adapter = FakeAdapter(responder=protocol_failure)
        service = InvocationService(self.database, adapter)
        with self.assertRaises(WorkerProtocolError):
            service.invoke(request, self._context(0.5))

        row = self._row(request.call_id)
        self.assertEqual(InvocationState.FAILED.value, row["state"])
        self.assertEqual(1, row["cost_unavailable"])
        self.assertIsNone(row["remote_cost"])
        summary = self.database.cost_summary("run-1")
        self.assertEqual(0.0, summary["confirmed_remote_cost_usd"])
        self.assertEqual(1, summary["cost_unavailable_calls"])
        self.assertEqual(1, len(adapter.invocations))

    def test_invalid_cost_parse_result_persists_unavailable_without_release(self) -> None:
        from agentflow.opencode_adapter import parse_opencode_failed_usage

        data = json.dumps(
            {"type": "step_finish", "part": {"tokens": {"input": 17}, "cost": -1}}
        ).encode("utf-8")
        parsed = parse_opencode_failed_usage(
            data, duration_ms=1, is_local=False, termination_reason="nonzero_exit"
        )

        def protocol_failure(_request: InvocationRequest) -> InvocationResult:
            raise WorkerProtocolError("remote worker failed", result=parsed)

        request = self._request("invalid-cost", 0.5)
        adapter = FakeAdapter(responder=protocol_failure)
        service = InvocationService(self.database, adapter)
        with self.assertRaises(WorkerProtocolError):
            service.invoke(request, self._context(0.5))

        row = self._row(request.call_id)
        self.assertEqual(InvocationState.FAILED.value, row["state"])
        self.assertEqual(1, row["cost_unavailable"])
        self.assertIsNone(row["remote_cost"])
        self.assertEqual(17, row["input_tokens"])
        summary = self.database.cost_summary("run-1")
        self.assertEqual(0.0, summary["confirmed_remote_cost_usd"])
        self.assertEqual(0.5, summary["reserved_remote_cost_usd"])
        self.assertEqual(1, summary["cost_unavailable_calls"])
        self.assertEqual(1, len(adapter.invocations))


if __name__ == "__main__":
    unittest.main()
