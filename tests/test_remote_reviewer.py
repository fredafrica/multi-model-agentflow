from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from agentflow.adapters import (
    AdapterRouter,
    InvocationIncompleteError,
    InvocationOutcomeUnknown,
    ModelUnavailableError,
    ProviderNotConfiguredError,
    ReviewerProtocolError,
)
from agentflow.authorization import issue_authorization
from agentflow.cli import main
from agentflow.contracts import (
    BudgetMode,
    BusinessImportance,
    DataSensitivity,
    InvocationRequest,
    InvocationResult,
    ModelAvailabilityState,
    ModelRef,
    OperationalSafety,
    PlanContract,
    RiskLevel,
    RunMode,
    TaskContract,
)
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.opencode_adapter import (
    REMOTE_REVIEWER_MAX_STEPS,
    RemoteOpenCodeReviewerAdapter,
    parse_opencode_json,
)
from agentflow.policies import invocation_decision, review_independence_decision
from agentflow.runner import REVIEWER_OUTPUT_PROTOCOL, Runner
from agentflow.serialization import canonical_json, plan_hash
from agentflow.states import InvocationState, TaskState
from agentflow.workspace import GitWorkspace


PROVIDER = "review-provider"
from resource_budget_fixtures import budgeted_request, resolved_output_stub, budgeted_plan_contract
MODEL_ID = "review-model"


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def remote_task(
    *,
    sensitivity: DataSensitivity = DataSensitivity.PUBLIC,
    importance: BusinessImportance = BusinessImportance.IMPORTANT,
) -> TaskContract:
    return TaskContract(
        task_id="remote-review",
        objective="Review the bounded change",
        risk_level=RiskLevel(
            importance, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE
        ),
        allowed_files=(),
        forbidden_actions=("publish", "modify files"),
        acceptance_criteria=("the deterministic gate passes",),
        data_sensitivity=sensitivity,
        implementation_model=ModelRef("fake", "coder", "1", "local-family", True),
        review_model=ModelRef(
            PROVIDER, MODEL_ID, "2026-09", "remote-family", False
        ),
        fallback_model=None,
        max_remote_cost=0,
        max_retry_count=0,
        escalation_conditions=("review unavailable",),
        expected_outputs=(),
    )


def remote_plan(task: TaskContract | None = None) -> PlanContract:
    actual = task or remote_task()
    return budgeted_plan_contract(
        plan_id="remote-plan",
        schema_version=1,
        version=1,
        run_mode=RunMode.MANAGED,
        budget_mode=BudgetMode.FIXED,
        max_remote_cost=0,
        emergency_reserve=0,
        privacy_policy_version="privacy-v2",
        tasks=(actual,),
        allowed_provider_ids=("fake", PROVIDER),
    )


def review_request(*, role: str = "review", read_only: bool = True) -> InvocationRequest:
    return budgeted_request(
        call_id="call-1",
        request_key="request-1",
        run_id="run-1",
        task_id="remote-review",
        role=role,
        model=ModelRef(PROVIDER, MODEL_ID, "2026-09", "remote-family", False),
        prompt='{"task_id":"remote-review"}',
        data_sensitivity=DataSensitivity.PUBLIC,
        read_only=read_only,
    )


class _CompletedProcess:
    pid = 41234
    returncode = 0

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return self.stdout, ""


@mock.patch('agentflow.opencode_adapter._read_output_config', new=resolved_output_stub)
class RemoteAdapterTests(unittest.TestCase):
    def test_command_is_an_argument_array_and_remote_permissions_deny_all_tools(self) -> None:
        events = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "session-1",
                        "part": {
                            "type": "text",
                            "text": '{"approved":true,"findings":[]}',
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "step_finish",
                        "part": {"tokens": {"input": 5, "output": 2}, "cost": 0.125},
                    }
                ),
            )
        )
        discovery = subprocess.CompletedProcess(
            ("opencode",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )
        captured: dict[str, object] = {}

        def popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            directory = Path(command[command.index("--dir") + 1])
            captured["directory_writable"] = bool(directory.stat().st_mode & 0o222)
            return _CompletedProcess(events)

        adapter = RemoteOpenCodeReviewerAdapter(
            PROVIDER,
            planned_models=(review_request().model,),
            opencode_command="opencode-stub",
        )
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen", side_effect=popen
        ):
            result = adapter.invoke(review_request())

        command = captured["command"]
        self.assertIsInstance(command, tuple)
        self.assertEqual(
            f"{PROVIDER}/{MODEL_ID}", command[command.index("--model") + 1]
        )
        self.assertNotIn("shell", captured["kwargs"])
        self.assertFalse(captured["directory_writable"])
        config = json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])
        permission = config["permission"]
        for name in (
            "read",
            "glob",
            "grep",
            "edit",
            "write",
            "bash",
            "shell",
            "external_directory",
            "webfetch",
            "websearch",
            "task",
            "subagent",
            "skill",
            "question",
        ):
            self.assertEqual("deny", permission[name])
        self.assertEqual([PROVIDER], config["enabled_providers"])
        reviewer_config = config["agent"]["agentflow-remote-reviewer"]
        self.assertEqual(REMOTE_REVIEWER_MAX_STEPS, reviewer_config["steps"])
        self.assertGreater(reviewer_config["steps"], 1)
        self.assertEqual(0.125, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_provider_and_model_identifiers_reject_shell_metacharacters(self) -> None:
        for provider in ("bad;provider", "bad provider", "-provider"):
            with self.assertRaises(ValueError):
                ModelRef(provider, MODEL_ID, "1")
        for model_id in ("bad;model", "bad model", "$(bad)", "-model"):
            with self.assertRaises(ValueError):
                ModelRef(PROVIDER, model_id, "1")

    def test_remote_role_and_read_only_are_enforced_before_discovery(self) -> None:
        adapter = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="unused")
        with mock.patch("subprocess.run") as discovery:
            with self.assertRaisesRegex(ValueError, "remote role denied"):
                adapter.invoke(review_request(role="implementation"))
            with self.assertRaisesRegex(ValueError, "remote role denied"):
                adapter.invoke(review_request(read_only=False))
        discovery.assert_not_called()

    def test_not_configured_and_model_unavailable_are_distinct(self) -> None:
        adapter = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="stub")
        missing_provider = subprocess.CompletedProcess(
            ("stub",), 1, stdout="", stderr="not configured"
        )
        with mock.patch("subprocess.run", return_value=missing_provider):
            with self.assertRaisesRegex(ProviderNotConfiguredError, "not configured"):
                adapter.require_model(review_request().model)

        other_model = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/other-model\n", stderr=""
        )
        with mock.patch("subprocess.run", return_value=other_model):
            with self.assertRaisesRegex(ModelUnavailableError, "model unavailable"):
                adapter.require_model(review_request().model)

    def test_discovery_is_not_callable_verification(self) -> None:
        discovered = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )
        adapter = RemoteOpenCodeReviewerAdapter(
            PROVIDER, planned_models=(review_request().model,), opencode_command="stub"
        )
        with mock.patch("subprocess.run", return_value=discovered) as run:
            record = adapter.discover()[0]
        self.assertEqual(ModelAvailabilityState.DISCOVERABLE, record.availability_state)
        self.assertFalse(record.available)
        self.assertEqual(("stub", "models", PROVIDER, "--pure"), run.call_args.args[0])

    def test_remote_missing_cost_is_not_zero(self) -> None:
        event = json.dumps(
            {"type": "text", "part": {"type": "text", "text": "review"}}
        )
        result = parse_opencode_json(event, duration_ms=3, is_local=False)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)
        local = parse_opencode_json(event, duration_ms=3, is_local=True)
        self.assertEqual(0, local.remote_cost)
        self.assertFalse(local.cost_unavailable)

    def test_timeout_remote_process_is_unknown_with_reason_and_not_retried(self) -> None:
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )

        class TimedOutProcess:
            pid = 41235
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(("stub",), 1)
                return "", ""

        process = TimedOutProcess()
        adapter = RemoteOpenCodeReviewerAdapter(
            PROVIDER, opencode_command="stub", timeout_seconds=1
        )
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen", return_value=process
        ) as popen, mock.patch(
            "agentflow.opencode_adapter.OpenCodeAdapter.cancel", return_value=True
        ):
            with self.assertRaises(InvocationOutcomeUnknown) as raised:
                adapter.invoke(review_request())
        self.assertEqual(1, popen.call_count)
        self.assertEqual("timeout", raised.exception.termination_reason)
        self.assertIsNotNone(raised.exception.result)
        self.assertTrue(raised.exception.result.cost_unavailable)

    def test_interrupted_remote_process_is_unknown_and_not_retried(self) -> None:
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )

        class InterruptedProcess:
            pid = 41236
            returncode = None

            def communicate(self, timeout=None):
                raise KeyboardInterrupt

        process = InterruptedProcess()
        adapter = RemoteOpenCodeReviewerAdapter(
            PROVIDER, opencode_command="stub", timeout_seconds=1
        )
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen", return_value=process
        ) as popen, mock.patch(
            "agentflow.opencode_adapter.OpenCodeAdapter.cancel", return_value=True
        ), mock.patch(
            "agentflow.opencode_adapter._bounded_drain", return_value=(b"", False)
        ):
            with self.assertRaisesRegex(InvocationOutcomeUnknown, "outcome unknown"):
                adapter.invoke(review_request())
        self.assertEqual(1, popen.call_count)

    def test_zero_exit_step_limit_stream_is_incomplete_not_unavailable(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )
        adapter = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="stub")
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen",
            return_value=_CompletedProcess(fixture.read_text(encoding="utf-8")),
        ):
            with self.assertRaises(InvocationIncompleteError):
                adapter.invoke(review_request())

    def test_nonzero_exit_step_limit_is_incomplete_not_unavailable(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )

        class NonZeroProcess:
            pid = 41237
            returncode = 1

            def communicate(self, timeout=None):
                return fixture.read_text(encoding="utf-8"), ""

        adapter = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="stub")
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen", return_value=NonZeroProcess()
        ):
            with self.assertRaises(InvocationIncompleteError) as raised:
                adapter.invoke(review_request())
        error = raised.exception
        self.assertEqual("step_limit_reached", error.failure_kind)
        result = error.result
        self.assertEqual("session-step-limit", result.provider_request_id)
        self.assertEqual((17, 9), (result.input_tokens, result.output_tokens))
        self.assertEqual(0.125, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_nonzero_exit_without_step_limit_signal_is_protocol_error(self) -> None:
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )
        stdout = json.dumps(
            {"type": "text", "sessionID": "s", "part": {"type": "text", "text": "partial"}}
        )

        class NonZeroProcess:
            pid = 41238
            returncode = 1

            def communicate(self, timeout=None):
                return stdout, "model unavailable"

        adapter = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="stub")
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen", return_value=NonZeroProcess()
        ):
            with self.assertRaises(ReviewerProtocolError):
                adapter.invoke(review_request())

    def test_nonzero_exit_reviewer_preserves_confirmed_usage_and_cost(self) -> None:
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )
        stdout = json.dumps(
            {
                "type": "step_finish",
                "sessionID": "sess-usage",
                "part": {"tokens": {"input": 17, "output": 9}, "cost": 0.125},
            }
        )

        class NonZeroProcess:
            pid = 41241
            returncode = 1

            def communicate(self, timeout=None):
                return stdout, "model unavailable"

        adapter = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="stub")
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen", return_value=NonZeroProcess()
        ):
            with self.assertRaises(ReviewerProtocolError) as raised:
                adapter.invoke(review_request())
        result = raised.exception.result
        self.assertIsNotNone(result)
        self.assertEqual((17, 9), (result.input_tokens, result.output_tokens))
        self.assertEqual("sess-usage", result.provider_request_id)
        self.assertEqual(0.125, result.remote_cost)
        self.assertFalse(result.cost_unavailable)
        self.assertEqual("nonzero_exit", result.raw_metadata["termination_reason"])

    def test_no_text_but_usage_reviewer_preserves_tokens(self) -> None:
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )
        stdout = json.dumps(
            {
                "type": "step_finish",
                "sessionID": "sess-no-text",
                "part": {"tokens": {"input": 5, "output": 3}},
            }
        )

        class NonZeroProcess:
            pid = 41242
            returncode = 1

            def communicate(self, timeout=None):
                return stdout, "model unavailable"

        adapter = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="stub")
        with mock.patch("subprocess.run", return_value=discovery), mock.patch(
            "subprocess.Popen", return_value=NonZeroProcess()
        ):
            with self.assertRaises(ReviewerProtocolError) as raised:
                adapter.invoke(review_request())
        result = raised.exception.result
        self.assertIsNotNone(result)
        self.assertEqual((5, 3), (result.input_tokens, result.output_tokens))
        self.assertEqual("sess-no-text", result.provider_request_id)
        self.assertIsNone(result.remote_cost)
        self.assertTrue(result.cost_unavailable)
        self.assertEqual("opencode_json_events", result.raw_metadata["token_source"])


class RemotePolicyTests(unittest.TestCase):
    def test_provider_authorization_role_privacy_and_budget_are_pre_call_gates(self) -> None:
        task = remote_task()
        plan = remote_plan(task)
        authorization = issue_authorization(plan)
        request = review_request()
        allowed = invocation_decision(
            request,
            task=task,
            plan=plan,
            authorization=authorization,
            estimated_remote_cost=0,
            remote_cost_spent=0,
        )
        self.assertTrue(allowed.allowed)

        provider_denied = invocation_decision(
            request,
            task=task,
            plan=plan,
            authorization=replace(authorization, authorized_provider_ids=("fake",)),
            estimated_remote_cost=0,
            remote_cost_spent=0,
        )
        self.assertIn("provider is outside the authorization", provider_denied.reasons)

        role_denied = invocation_decision(
            replace(request, role="revision", read_only=False),
            task=task,
            plan=plan,
            authorization=authorization,
            estimated_remote_cost=0,
            remote_cost_spent=0,
        )
        self.assertTrue(any("remote role denied" in item for item in role_denied.reasons))

        d3_task = remote_task(sensitivity=DataSensitivity.STRICTLY_PRIVATE)
        d3_plan = remote_plan(d3_task)
        d3 = invocation_decision(
            replace(request, data_sensitivity=DataSensitivity.STRICTLY_PRIVATE),
            task=d3_task,
            plan=d3_plan,
            authorization=issue_authorization(
                d3_plan, allow_d1_remote=True, allow_d2_remote=True
            ),
            estimated_remote_cost=0,
            remote_cost_spent=0,
            redaction_passed=True,
        )
        self.assertTrue(any("privacy denied" in item for item in d3.reasons))

        over_budget = invocation_decision(
            request,
            task=task,
            plan=plan,
            authorization=authorization,
            estimated_remote_cost=0.01,
            remote_cost_spent=0,
        )
        self.assertTrue(any("budget denied" in item for item in over_budget.reasons))

    def test_same_version_and_b2_b3_same_family_review_are_denied(self) -> None:
        base = remote_task()
        same_version = replace(base, review_model=base.implementation_model)
        self.assertFalse(review_independence_decision(same_version).allowed)
        for importance in (BusinessImportance.IMPORTANT, BusinessImportance.CRITICAL):
            same_family = replace(
                base,
                risk_level=replace(base.risk_level, business_importance=importance),
                review_model=replace(base.review_model, family="local-family"),
            )
            self.assertFalse(review_independence_decision(same_family).allowed)

    def test_important_review_requires_explicit_family_metadata(self) -> None:
        task = remote_task()
        without_family = replace(
            task,
            review_model=replace(task.review_model, family=None),
        )
        decision = review_independence_decision(without_family)
        self.assertFalse(decision.allowed)
        self.assertIn("family metadata", decision.reasons[0])


class CostAuditTests(unittest.TestCase):
    def test_confirmed_remote_cost_and_unknown_cost_are_aggregated_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "agentflow.db")
            database.initialize()
            plan = replace(remote_plan(), max_remote_cost=2)
            authorization = issue_authorization(plan)
            database.save_plan(plan)
            database.save_authorization(authorization)
            database.create_run("cost-run", plan, authorization)
            database.transition_task(
                "cost-run", "remote-review", TaskState.WAITING_AUTHORIZATION
            )
            database.transition_task("cost-run", "remote-review", TaskState.QUEUED)
            database.transition_task("cost-run", "remote-review", TaskState.RUNNING)
            database.create_attempt("cost-attempt", "cost-run", "remote-review", 1)
            try:
                for suffix, result in (
                    (
                        "known",
                        InvocationResult("session-known", "ok", 10, 4, 1, 2, 0.25),
                    ),
                    (
                        "unknown",
                        InvocationResult(
                            "session-unknown", "ok", 8, 3, 1, 2, None
                        ),
                    ),
                ):
                    request = replace(
                        review_request(),
                        call_id=f"call-{suffix}",
                        request_key=f"request-{suffix}",
                        run_id="cost-run",
                        metadata={
                            "estimated_remote_cost": 0.4 if suffix == "unknown" else 0
                        },
                    )
                    database.register_call(request, "cost-attempt")
                    database.transition_call(request.call_id, InvocationState.STARTED)
                    database.complete_call(request.call_id, result, "cost-run")
                summary = database.cost_summary("cost-run")
                committed = database.remote_budget_committed("cost-run")
                unknown = database.fetch_one(
                    "SELECT remote_cost, cost_unavailable FROM model_calls "
                    "WHERE call_id = 'call-unknown'"
                )
                entries = database.fetch_one(
                    "SELECT COUNT(*) AS count FROM cost_entries"
                )
            finally:
                database.close()
        self.assertEqual(0.25, summary["confirmed_remote_cost_usd"])
        self.assertEqual(0.65, committed)
        self.assertEqual(1, summary["cost_unavailable_calls"])
        self.assertEqual(2, summary["by_provider_model"][0]["calls"])
        self.assertIsNone(unknown["remote_cost"])
        self.assertEqual(1, unknown["cost_unavailable"])
        self.assertEqual(1, entries["count"])


@mock.patch('agentflow.opencode_adapter._read_output_config', new=resolved_output_stub)
class ReviewPacketTests(unittest.TestCase):
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

    def test_packet_is_minimal_hashed_and_audited_without_body(self) -> None:
        task = remote_task()
        plan = remote_plan(task)

        def remote_review(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                "stub-session",
                json.dumps({"approved": True, "findings": []}),
                7,
                3,
                1,
                2,
                0,
                {"test_double": True},
            )

        local = FakeAdapter(
            responder=lambda request: InvocationResult(
                "fake-local", "implementation complete", 0, 0, 0, 0, 0,
                {"test_double": True}
            )
        )
        remote = FakeAdapter(responder=remote_review)
        result = Runner(
            self.database,
            AdapterRouter({"fake": local, PROVIDER: remote}),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="packet-run")
        self.assertEqual("completed", result.state.value)
        request = remote.invocations[0]
        self.assertTrue(request.prompt.startswith(REVIEWER_OUTPUT_PROTOCOL))
        packet_json = request.prompt.removeprefix(REVIEWER_OUTPUT_PROTOCOL)
        packet = json.loads(packet_json)
        self.assertEqual(
            {
                "task_id",
                "objective",
                "risk_level",
                "acceptance_criteria",
                "forbidden_actions",
                "diff",
                "deterministic_test",
                "evidence_excerpts",
                "input_artifacts",
                "review_questions",
                "expected_response",
            },
            set(packet),
        )
        self.assertNotIn("implementation complete", request.prompt)
        packet_bytes = packet_json.encode("utf-8")
        self.assertEqual(hashlib.sha256(packet_bytes).hexdigest(), request.metadata["packet_hash"])
        self.assertEqual(len(packet_bytes), request.metadata["packet_size"])
        row = self.database.fetch_one(
            "SELECT request_scope_json, test_double FROM model_calls WHERE role = 'review'"
        )
        audit = json.loads(row["request_scope_json"])
        self.assertEqual(request.metadata["packet_hash"], audit["packet_hash"])
        self.assertEqual("privacy-v2", audit["privacy_policy_version"])
        self.assertNotIn("objective", audit)
        self.assertEqual(1, row["test_double"])

    def test_prompt_explicitly_requires_json_only_output(self) -> None:
        protocol = REVIEWER_OUTPUT_PROTOCOL.lower()
        self.assertIn("exactly one json object", protocol)
        self.assertIn("nothing else", protocol)
        self.assertIn("do not use a markdown code fence", protocol)
        self.assertIn("approved (boolean)", protocol)
        self.assertIn("findings (array)", protocol)

    def test_remote_review_with_unavailable_cost_wakes_supervisor(self) -> None:
        task = remote_task()
        plan = remote_plan(task)

        def remote_review(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                "stub-session",
                json.dumps({"approved": True, "findings": []}),
                7,
                3,
                1,
                2,
                None,
                {"test_double": True},
            )

        local = FakeAdapter(
            responder=lambda request: InvocationResult(
                "fake-local", "implementation complete", 0, 0, 0, 0, 0,
                {"test_double": True}
            )
        )
        remote = FakeAdapter(responder=remote_review)
        result = Runner(
            self.database,
            AdapterRouter({"fake": local, PROVIDER: remote}),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="cost-wake-run")
        self.assertEqual("completed", result.state.value)
        reasons = [
            checkpoint["reason"]
            for checkpoint in self.database.pending_supervisor_checkpoints("cost-wake-run")
        ]
        self.assertIn("cost_unavailable", reasons)

    def test_untracked_planned_output_enters_scope_packet_and_snapshot_stays_stable(self) -> None:
        relative = "outputs/new-result.txt"
        task = replace(
            remote_task(),
            allowed_files=(relative,),
            expected_outputs=(relative,),
        )
        plan = remote_plan(task)
        reviewer_snapshots: list[tuple[str, str]] = []

        def local_response(request: InvocationRequest) -> InvocationResult:
            path = Path(request.metadata["worktree"]) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("planned untracked output\n", encoding="utf-8")
            return InvocationResult(
                f"fake:{request.request_key}",
                "implementation complete",
                0,
                0,
                0,
                1,
                0,
                {"test_double": True},
            )

        def remote_response(request: InvocationRequest) -> InvocationResult:
            root = Path(request.metadata["worktree"])
            before = self.workspace.status_snapshot(root)
            result = InvocationResult(
                f"fake:{request.request_key}",
                json.dumps({"approved": True, "findings": []}),
                1,
                1,
                0,
                1,
                0,
                {"test_double": True},
            )
            reviewer_snapshots.append((before, self.workspace.status_snapshot(root)))
            return result

        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=local_response),
                    PROVIDER: FakeAdapter(responder=remote_response),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="untracked-packet-run")
        self.assertEqual("completed", result.state.value)
        review_call = self.database.fetch_one(
            "SELECT output_text FROM model_calls WHERE role = 'review'"
        )
        self.assertIsNotNone(review_call)
        worktree = Path(
            self.database.run_snapshot("untracked-packet-run")["tasks"][0][
                "worktree_path"
            ]
        )
        self.assertIn(relative, self.workspace.changed_files(worktree))
        self.assertTrue((worktree / relative).is_file())
        self.assertIn(f"+++ b/{relative}", self.workspace.diff(worktree))
        self.assertIn("planned untracked output", self.workspace.diff(worktree))
        self.assertEqual(1, len(reviewer_snapshots))
        self.assertEqual(*reviewer_snapshots[0])

    def test_unavailable_primary_reviewer_uses_independent_fallback(self) -> None:
        task = replace(
            remote_task(),
            fallback_model=ModelRef(
                "fake", "backup-reviewer", "1", "backup-family", True
            ),
        )
        plan = remote_plan(task)

        def local_response(request: InvocationRequest) -> InvocationResult:
            output = (
                json.dumps({"approved": True, "findings": []})
                if request.role in {"review", "rereview"}
                else "implementation complete"
            )
            return InvocationResult(
                f"fake:{request.request_key}", output, 0, 0, 0, 0, 0,
                {"test_double": True}
            )

        def unavailable(_request: InvocationRequest) -> InvocationResult:
            raise ProviderNotConfiguredError("provider not configured")

        local = FakeAdapter(responder=local_response)
        remote = FakeAdapter(responder=unavailable)
        result = Runner(
            self.database,
            AdapterRouter({"fake": local, PROVIDER: remote}),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="fallback-review-run")
        self.assertEqual("completed", result.state.value)
        self.assertEqual(["implementation", "review"], [item.role for item in local.invocations])
        self.assertEqual(1, len(remote.invocations))

    def test_same_family_fallback_is_not_used_and_task_waits_for_review(self) -> None:
        task = replace(
            remote_task(),
            fallback_model=ModelRef(
                "fake", "same-family-reviewer", "1", "local-family", True
            ),
        )
        plan = remote_plan(task)

        def local_response(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                "fake-local", "implementation complete", 0, 0, 0, 0, 0,
                {"test_double": True}
            )

        def unavailable(_request: InvocationRequest) -> InvocationResult:
            raise ProviderNotConfiguredError("provider not configured")

        local = FakeAdapter(responder=local_response)
        result = Runner(
            self.database,
            AdapterRouter(
                {"fake": local, PROVIDER: FakeAdapter(responder=unavailable)}
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="fallback-denied-run")
        self.assertEqual("paused", result.state.value)
        self.assertEqual(
            TaskState.WAITING_REVIEW,
            self.database.task_state("fallback-denied-run", task.task_id),
        )
        self.assertEqual(["implementation"], [item.role for item in local.invocations])

    def test_independent_fallback_replaces_same_family_primary_without_calling_it(self) -> None:
        task = replace(
            remote_task(),
            review_model=ModelRef(
                PROVIDER, MODEL_ID, "2026-09", "local-family", False
            ),
            fallback_model=ModelRef(
                "fake", "independent-reviewer", "1", "backup-family", True
            ),
        )
        plan = remote_plan(task)

        def local_response(request: InvocationRequest) -> InvocationResult:
            output = (
                json.dumps({"approved": True, "findings": []})
                if request.role == "review"
                else "implementation complete"
            )
            return InvocationResult(
                f"fake:{request.request_key}",
                output,
                0,
                0,
                0,
                0,
                0,
                {"test_double": True},
            )

        local = FakeAdapter(responder=local_response)
        primary = FakeAdapter()
        result = Runner(
            self.database,
            AdapterRouter({"fake": local, PROVIDER: primary}),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="independent-fallback-run")
        self.assertEqual("completed", result.state.value)
        self.assertEqual([], primary.invocations)

    def test_structured_findings_include_title_explanation_path_and_remediation(self) -> None:
        reviewer = remote_task().review_model
        review = Runner._parse_review(
            remote_task(),
            reviewer,
            json.dumps(
                {
                    "approved": True,
                    "findings": [
                        {
                            "severity": "P2",
                            "title": "Clarify message",
                            "explanation": "The error is technically correct but vague.",
                            "path": "src/example.py",
                            "remediation": "Use a provider-specific message.",
                        }
                    ],
                }
            ),
        )
        finding = review.findings[0]
        self.assertEqual("Clarify message", finding.title)
        self.assertEqual("src/example.py", finding.path)
        self.assertEqual("Use a provider-specific message.", finding.remediation)

    def test_p0_or_p1_finding_deterministically_overrides_approved_true(self) -> None:
        review = Runner._parse_review(
            remote_task(),
            remote_task().review_model,
            json.dumps(
                {
                    "approved": True,
                    "findings": [
                        {
                            "severity": "P1",
                            "title": "Acceptance failure",
                            "explanation": "One criterion is not met.",
                        }
                    ],
                }
            ),
        )
        self.assertFalse(review.approved)

    def test_quoted_step_limit_creates_blocking_review_record(self) -> None:
        task = remote_task()
        plan = remote_plan(task)
        marker = "The maximum number of steps for this agent has been reached."

        def local_response(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                f"fake:{request.request_key}",
                "implementation complete",
                0,
                0,
                0,
                1,
                0,
                {"test_double": True},
            )

        def remote_response(request: InvocationRequest) -> InvocationResult:
            review = json.dumps(
                {
                    "approved": True,
                    "findings": [
                        {
                            "severity": "P1",
                            "title": "Worker stopped early",
                            "explanation": marker,
                        }
                    ],
                }
            )
            event = json.dumps(
                {"type": "text", "part": {"type": "text", "text": review}}
            )
            return parse_opencode_json(event, duration_ms=1, is_local=False)

        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=local_response),
                    PROVIDER: FakeAdapter(responder=remote_response),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="quoted-step-limit-run")

        self.assertEqual("paused", result.state.value)
        review = self.database.fetch_one(
            "SELECT approved, findings_json FROM reviews WHERE run_id = ?",
            ("quoted-step-limit-run",),
        )
        self.assertIsNotNone(review)
        self.assertEqual(0, review["approved"])
        self.assertEqual("P1", json.loads(review["findings_json"])[0]["severity"])
        checkpoint = self.database.run_snapshot("quoted-step-limit-run")["run"][
            "checkpoint_json"
        ]
        if checkpoint is not None:
            self.assertNotEqual(
                "review_step_limit_reached", json.loads(checkpoint).get("reason")
            )

    def test_noncompliant_reviewer_outputs_are_rejected(self) -> None:
        invalid = (
            json.dumps([]),
            "```json\n{\"approved\":true,\"findings\":[]}\n```",
            'prefix {"approved":true,"findings":[]} suffix',
            json.dumps({"approved": True}),
            json.dumps({"approved": "true", "findings": []}),
            json.dumps({"approved": True, "findings": {}}),
            json.dumps(
                {
                    "approved": False,
                    "findings": [
                        {"severity": "P4", "title": "bad", "explanation": "bad"}
                    ],
                }
            ),
            json.dumps(
                {
                    "approved": False,
                    "findings": [{"severity": "P1", "title": "missing"}],
                }
            ),
        )
        for output in invalid:
            with self.subTest(output=output):
                with self.assertRaises((json.JSONDecodeError, ValueError)):
                    Runner._parse_review(
                        remote_task(), remote_task().review_model, output
                    )

    def test_invalid_output_pauses_without_review_record(self) -> None:
        task = remote_task()
        plan = remote_plan(task)

        def respond(request: InvocationRequest) -> InvocationResult:
            output = (
                "implementation complete"
                if request.role == "implementation"
                else "```json\n{\"approved\":true,\"findings\":[]}\n```"
            )
            return InvocationResult(
                f"fake:{request.request_key}",
                output,
                2,
                1,
                0,
                1,
                0,
                {"test_double": True},
            )

        result = Runner(
            self.database,
            AdapterRouter(
                {"fake": FakeAdapter(responder=respond), PROVIDER: FakeAdapter(responder=respond)}
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="invalid-review-run")
        self.assertEqual("paused", result.state.value)
        checkpoint = json.loads(
            self.database.run_snapshot("invalid-review-run")["run"]["checkpoint_json"]
        )
        self.assertEqual("reviewer_output_invalid", checkpoint["reason"])
        self.assertEqual(
            0,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )

    def test_remote_step_limit_preserves_usage_and_pauses_without_review(self) -> None:
        task = remote_task()
        plan = replace(remote_plan(task), max_remote_cost=1)

        def local_response(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                f"fake:{request.request_key}",
                "implementation complete",
                0,
                0,
                0,
                1,
                0,
                {"test_double": True},
            )

        def incomplete(_request: InvocationRequest) -> InvocationResult:
            fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
            return parse_opencode_json(
                fixture.read_text(encoding="utf-8"),
                duration_ms=41,
                is_local=False,
            )

        local = FakeAdapter(responder=local_response)
        remote = FakeAdapter(responder=incomplete)
        runner = Runner(
            self.database,
            AdapterRouter({"fake": local, PROVIDER: remote}),
            self.workspace,
        )
        authorization = issue_authorization(plan)
        result = runner.start(plan, authorization, run_id="review-step-limit-run")
        self.assertEqual("paused", result.state.value)
        checkpoint = json.loads(
            self.database.run_snapshot("review-step-limit-run")["run"]["checkpoint_json"]
        )
        self.assertEqual("review_step_limit_reached", checkpoint["reason"])
        call = self.database.fetch_one(
            "SELECT state, input_tokens, output_tokens, duration_ms, remote_cost, "
            "raw_metadata_json, test_double FROM model_calls WHERE role = 'review'"
        )
        self.assertEqual("failed", call["state"])
        self.assertEqual(
            (17, 9, 41, 0.125),
            tuple(
                call[key]
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "duration_ms",
                    "remote_cost",
                )
            ),
        )
        self.assertEqual(
            "step_limit_reached",
            json.loads(call["raw_metadata_json"])["failure_kind"],
        )
        self.assertEqual(1, call["test_double"])
        self.assertEqual(
            0,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )
        self.assertEqual(
            0.125,
            self.database.cost_summary("review-step-limit-run")[
                "confirmed_remote_cost_usd"
            ],
        )
        with self.assertRaisesRegex(ValueError, "cannot be continued"):
            runner.resume("review-step-limit-run", plan, authorization)
        self.assertEqual(1, len(remote.invocations))

    def test_nonzero_exit_reviewer_step_limit_pauses_without_review_row(self) -> None:
        task = remote_task()
        plan = replace(remote_plan(task), max_remote_cost=1)
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        stdout = fixture.read_text(encoding="utf-8")
        discovery = subprocess.CompletedProcess(
            ("stub",), 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr=""
        )

        class NonZeroProcess:
            pid = 41239
            returncode = 1

            def communicate(self, timeout=None):
                return stdout, ""

        def local_response(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                f"fake:{request.request_key}",
                "implementation complete",
                0,
                0,
                0,
                1,
                0,
                {"test_double": True},
            )

        local = FakeAdapter(responder=local_response)
        remote = RemoteOpenCodeReviewerAdapter(PROVIDER, opencode_command="stub")
        runner = Runner(
            self.database,
            AdapterRouter({"fake": local, PROVIDER: remote}),
            self.workspace,
        )
        authorization = issue_authorization(plan)
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def run_side_effect(args, **kwargs):
            if args and args[0] == "stub":
                return discovery
            return real_run(args, **kwargs)

        def popen_side_effect(command, **kwargs):
            if command and command[0] == "stub":
                return NonZeroProcess()
            return real_popen(command, **kwargs)

        with mock.patch("subprocess.run", side_effect=run_side_effect), mock.patch(
            "subprocess.Popen", side_effect=popen_side_effect
        ):
            result = runner.start(
                plan, authorization, run_id="review-nonzero-step-limit-run"
            )
        self.assertEqual("paused", result.state.value)
        checkpoint = json.loads(
            self.database.run_snapshot("review-nonzero-step-limit-run")["run"][
                "checkpoint_json"
            ]
        )
        self.assertEqual("review_step_limit_reached", checkpoint["reason"])
        self.assertEqual(
            0,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )
        call = self.database.fetch_one(
            "SELECT state, input_tokens, output_tokens, remote_cost, "
            "cost_unavailable FROM model_calls WHERE role = 'review'"
        )
        self.assertEqual("failed", call["state"])
        self.assertEqual((17, 9, 0.125), tuple(
            call[key] for key in ("input_tokens", "output_tokens", "remote_cost")
        ))
        self.assertEqual(0, call["cost_unavailable"])
        with self.assertRaisesRegex(ValueError, "cannot be continued"):
            runner.resume("review-nonzero-step-limit-run", plan, authorization)
        self.assertEqual(1, len(local.invocations))


@mock.patch('agentflow.opencode_adapter._read_output_config', new=resolved_output_stub)
class RemoteCliStubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / ".gitignore").write_text(".agentflow/runs/\n", encoding="utf-8")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", ".gitignore", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        policy = self.root / ".agentflow"
        policy.mkdir()
        self.plan = remote_plan()
        (policy / "plan.json").write_text(canonical_json(self.plan), encoding="utf-8")
        self.stub = self.root / "opencode-stub"
        self.stub.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "if sys.argv[1] == 'models':\n"
            f"    print('{PROVIDER}/{MODEL_ID}')\n"
            "elif sys.argv[1] == 'run':\n"
            "    print(json.dumps({'type':'text','sessionID':'stub-session','part':"
            "{'type':'text','text':'{\\\"approved\\\":true,\\\"findings\\\":[]}'}}))\n"
            "    print(json.dumps({'type':'step_finish','part':"
            "{'tokens':{'input':4,'output':2},'cost':0}}))\n",
            encoding="utf-8",
        )
        self.stub.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--project", str(self.root), *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_cli_remote_reviewer_uses_stub_and_records_zero_reported_cost(self) -> None:
        factory = lambda provider, planned_models: RemoteOpenCodeReviewerAdapter(
            provider,
            planned_models=planned_models,
            opencode_command=str(self.stub),
            test_double=True,
        )
        with mock.patch(
            "agentflow.cli.RemoteOpenCodeReviewerAdapter", side_effect=factory
        ):
            code, _, error = self.call(
                "plan", "authorize", "--hash", plan_hash(self.plan)
            )
            self.assertEqual(0, code, error)
            code, output, error = self.call(
                "start", self.plan.plan_id, "--run-id", "remote-cli-run"
            )
            self.assertEqual(0, code, error)
            self.assertEqual("completed", json.loads(output)["state"])
            code, output, error = self.call("cost", "remote-cli-run")
            self.assertEqual(0, code, error)
        summary = json.loads(output)
        self.assertEqual(0, summary["confirmed_remote_cost_usd"])
        self.assertEqual(0, summary["cost_unavailable_calls"])
        database = sqlite3.connect(
            self.root / ".agentflow" / "runs" / "agentflow.db"
        )
        try:
            review = database.execute(
                "SELECT remote_cost, cost_unavailable, test_double FROM model_calls "
                "WHERE role = 'review'"
            ).fetchone()
        finally:
            database.close()
        self.assertEqual((0.0, 0, 1), review)

    def test_not_configured_provider_fails_before_run_creation(self) -> None:
        code, _, error = self.call(
            "plan", "authorize", "--hash", plan_hash(self.plan)
        )
        self.assertEqual(0, code, error)
        with mock.patch(
            "agentflow.cli.RemoteOpenCodeReviewerAdapter",
            side_effect=ProviderNotConfiguredError(
                f"provider not configured: {PROVIDER}"
            ),
        ):
            code, _, error = self.call("start", self.plan.plan_id)
        self.assertEqual(2, code)
        self.assertIn("provider not configured", error)
        connection = sqlite3.connect(
            self.root / ".agentflow" / "runs" / "agentflow.db"
        )
        try:
            runs = connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(0, runs)

    def test_remote_implementation_is_denied_before_adapter_discovery(self) -> None:
        task = replace(
            remote_task(),
            implementation_model=ModelRef(
                PROVIDER, "implementation-model", "1", "remote-family", False
            ),
            review_model=ModelRef(
                "fake", "local-reviewer", "1", "review-family", True
            ),
        )
        plan = remote_plan(task)
        (self.root / ".agentflow" / "plan.json").write_text(
            canonical_json(plan), encoding="utf-8"
        )
        code, _, error = self.call(
            "plan", "authorize", "--hash", plan_hash(plan)
        )
        self.assertEqual(0, code, error)
        with mock.patch("agentflow.cli.RemoteOpenCodeReviewerAdapter") as remote:
            code, _, error = self.call("start", plan.plan_id)
        self.assertEqual(2, code)
        self.assertIn("remote role denied", error)
        remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
