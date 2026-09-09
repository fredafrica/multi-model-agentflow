from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

import agentflow.workspace as workspace_module
from agentflow.adapters import (
    AdapterRouter,
    InvocationIncompleteError,
    InvocationOutcomeUnknown,
)
from agentflow.authorization import issue_authorization
from agentflow.cli import main
from agentflow.contracts import (
    BudgetMode,
    BusinessImportance,
    DataSensitivity,
    InputArtifact,
    InvocationRequest,
    InvocationResult,
    ModelRef,
    OperationalSafety,
    PlanContract,
    RemoteNetworkMode,
    ReviewAcceptancePolicy,
    RiskLevel,
    RunMode,
    SupervisorPolicy,
    TaskContract,
)
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.opencode_adapter import RemoteOpenCodeWorkerAdapter
from agentflow.policies import invocation_decision, network_decision
from agentflow.runner import Runner
from agentflow.serialization import canonical_json, digest_sha256, load_plan_json, plan_hash
from agentflow.states import RunState, TaskState
from agentflow.workspace import GitWorkspace, InputArtifactError, StagingSyncError

WORKER_PROVIDER = "worker-provider"
from resource_budget_fixtures import budgeted_request, resolved_output_stub, budgeted_plan_contract
WORKER_MODEL = "worker-model"


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def worker_task(
    *,
    task_id: str = "remote-impl",
    allow_remote: bool = True,
    input_artifacts: tuple[InputArtifact, ...] = (),
    review_policy: ReviewAcceptancePolicy = ReviewAcceptancePolicy.BLOCK_P0_P1,
    network_mode: RemoteNetworkMode = RemoteNetworkMode.DENY,
    allowed_hosts: tuple[str, ...] = (),
) -> TaskContract:
    relative = f"outputs/{task_id}.txt"
    return TaskContract(
        task_id=task_id,
        objective=f"Create {relative}",
        risk_level=RiskLevel(
            BusinessImportance.NORMAL, OperationalSafety.REVERSIBLE_OR_PUBLIC_REMOTE
        ),
        allowed_files=(relative,),
        forbidden_actions=("network",),
        acceptance_criteria=(f"{relative} exists",),
        data_sensitivity=DataSensitivity.PUBLIC,
        implementation_model=ModelRef(WORKER_PROVIDER, WORKER_MODEL, "2026-09", "worker-family", False),
        review_model=ModelRef("fake", "reviewer", "1", "reviewer-family", True),
        fallback_model=None,
        max_remote_cost=1,
        max_retry_count=0,
        escalation_conditions=("implementation failure",),
        expected_outputs=(relative,),
        test_command=("/bin/sh", "-c", f"test -f {relative}"),
        allow_remote_implementation=allow_remote,
        remote_worker_network_mode=network_mode,
        remote_worker_allowed_hosts=allowed_hosts,
        input_artifacts=input_artifacts,
        review_acceptance_policy=review_policy,
    )


def worker_plan(
    task: TaskContract | None = None,
    supervisor_policy: SupervisorPolicy | None = None,
    allowed_provider_ids: tuple[str, ...] = ("fake", WORKER_PROVIDER),
) -> PlanContract:
    actual = task or worker_task()
    return budgeted_plan_contract(
        plan_id="worker-plan",
        schema_version=1,
        version=1,
        run_mode=RunMode.MANAGED,
        budget_mode=BudgetMode.FIXED,
        max_remote_cost=1,
        emergency_reserve=0,
        privacy_policy_version="1",
        tasks=(actual,),
        allowed_provider_ids=allowed_provider_ids,
        supervisor_policy=supervisor_policy or SupervisorPolicy(),
    )


def worker_request(*, role: str = "implementation", read_only: bool = False) -> InvocationRequest:
    return budgeted_request(
        call_id="call-1",
        request_key="request-1",
        run_id="run-1",
        task_id="remote-impl",
        role=role,
        model=ModelRef(WORKER_PROVIDER, WORKER_MODEL, "2026-09", "worker-family", False),
        prompt="objective",
        data_sensitivity=DataSensitivity.PUBLIC,
        read_only=read_only,
    )


class _CompletedProcess:
    pid = 51234
    returncode = 0

    def __init__(self, stdout: str) -> None:
        self.stdout = stdout

    def communicate(self, timeout: int | None = None) -> tuple[str, str]:
        return self.stdout, ""


class ContractTests(unittest.TestCase):
    def test_input_artifact_validation(self) -> None:
        with self.assertRaises(ValueError):
            InputArtifact(path="../secret.txt", sha256="a" * 64)
        with self.assertRaises(ValueError):
            InputArtifact(path="/abs/secret.txt", sha256="a" * 64)
        with self.assertRaises(ValueError):
            InputArtifact(path="seed.txt", sha256="not-hex")
        artifact = InputArtifact(path="seed.txt", sha256="A" * 64)
        self.assertEqual("a" * 64, artifact.sha256)

    def test_supervisor_policy_validation(self) -> None:
        with self.assertRaises(ValueError):
            SupervisorPolicy(max_checkpoint_chars=10)
        with self.assertRaises(ValueError):
            SupervisorPolicy(max_supervisor_checkpoints=0)
        with self.assertRaises(ValueError):
            SupervisorPolicy(continuous_llm_monitoring=True)
        with self.assertRaises(ValueError):
            SupervisorPolicy(supervisor_model_hint="   ")
        with self.assertRaises(ValueError):
            SupervisorPolicy(supervisor_model_hint=7)

    def test_supervisor_model_hint_round_trip(self) -> None:
        policy = SupervisorPolicy(
            wake_events=("p0_p1_finding",),
            supervisor_model_hint="generic/supervisor-hint",
        )
        reloaded = load_plan_json(
            canonical_json(worker_plan(supervisor_policy=policy))
        )
        self.assertEqual(
            "generic/supervisor-hint",
            reloaded.supervisor_policy.supervisor_model_hint,
        )

    def test_input_artifact_must_not_overlap_allowed_files(self) -> None:
        base = worker_task()
        with self.assertRaises(ValueError):
            replace(
                base,
                input_artifacts=(InputArtifact("outputs/remote-impl.txt", "a" * 64),),
            )

    def test_remote_worker_host_validation(self) -> None:
        with self.assertRaises(ValueError):
            worker_task(allowed_hosts=("bad host",))
        with self.assertRaises(ValueError):
            worker_task(allowed_hosts=("-bad.example",))

    def test_plan_rejects_unknown_supervisor_wake_event(self) -> None:
        with self.assertRaises(ValueError):
            worker_plan(
                supervisor_policy=SupervisorPolicy(wake_events=("not_a_real_event",))
            )

    def test_new_fields_round_trip_through_canonical_json(self) -> None:
        task = worker_task(
            input_artifacts=(InputArtifact("inputs/seed.txt", "a" * 64),),
            review_policy=ReviewAcceptancePolicy.ZERO_FINDINGS,
        )
        plan = worker_plan(
            task,
            supervisor_policy=SupervisorPolicy(
                wake_events=("p0_p1_finding", "review_retries_exhausted")
            ),
        )
        reloaded = load_plan_json(canonical_json(plan))
        self.assertEqual(plan_hash(plan), plan_hash(reloaded))
        reloaded_task = reloaded.tasks[0]
        self.assertTrue(reloaded_task.allow_remote_implementation)
        self.assertEqual(RemoteNetworkMode.DENY, reloaded_task.remote_worker_network_mode)
        self.assertEqual(
            "a" * 64, reloaded_task.input_artifacts[0].sha256
        )
        self.assertEqual(
            ReviewAcceptancePolicy.ZERO_FINDINGS, reloaded_task.review_acceptance_policy
        )
        self.assertEqual(
            ("p0_p1_finding", "review_retries_exhausted"),
            reloaded.supervisor_policy.wake_events,
        )


class NetworkDecisionTests(unittest.TestCase):
    def test_deny_mode_is_always_denied(self) -> None:
        decision = network_decision(RemoteNetworkMode.DENY, (), "example.com")
        self.assertFalse(decision.allowed)

    def test_allowlist_matches_exact_and_subdomain(self) -> None:
        self.assertTrue(
            network_decision(
                RemoteNetworkMode.ALLOWLIST, ("example.com",), "example.com"
            ).allowed
        )
        self.assertTrue(
            network_decision(
                RemoteNetworkMode.ALLOWLIST, ("example.com",), "api.example.com"
            ).allowed
        )

    def test_allowlist_fails_closed_on_unknown_host(self) -> None:
        decision = network_decision(
            RemoteNetworkMode.ALLOWLIST, ("example.com",), "evil.com"
        )
        self.assertFalse(decision.allowed)

    def test_allowlist_requires_a_target_host(self) -> None:
        decision = network_decision(RemoteNetworkMode.ALLOWLIST, ("example.com",), "")
        self.assertFalse(decision.allowed)


class RemoteWorkerPolicyTests(unittest.TestCase):
    def test_remote_worker_allowed_only_when_authorized(self) -> None:
        task = worker_task()
        plan = worker_plan(task)
        authorization = issue_authorization(plan)
        allowed = invocation_decision(
            worker_request(),
            task=task,
            plan=plan,
            authorization=authorization,
            estimated_remote_cost=0.1,
            remote_cost_spent=0,
        )
        self.assertTrue(allowed.allowed)

    def test_remote_worker_denied_when_not_authorized(self) -> None:
        task = worker_task(allow_remote=False)
        plan = worker_plan(task)
        denied = invocation_decision(
            worker_request(),
            task=task,
            plan=plan,
            authorization=issue_authorization(plan),
            estimated_remote_cost=0.1,
            remote_cost_spent=0,
        )
        self.assertTrue(any("remote role denied" in item for item in denied.reasons))

    def test_remote_worker_denied_when_read_only(self) -> None:
        task = worker_task()
        plan = worker_plan(task)
        denied = invocation_decision(
            worker_request(read_only=True),
            task=task,
            plan=plan,
            authorization=issue_authorization(plan),
            estimated_remote_cost=0.1,
            remote_cost_spent=0,
        )
        self.assertTrue(any("write role" in item for item in denied.reasons))

    def test_remote_worker_allowlist_mode_fails_closed(self) -> None:
        task = worker_task(
            network_mode=RemoteNetworkMode.ALLOWLIST, allowed_hosts=("example.com",)
        )
        plan = worker_plan(task)
        denied = invocation_decision(
            worker_request(),
            task=task,
            plan=plan,
            authorization=issue_authorization(plan),
            estimated_remote_cost=0.1,
            remote_cost_spent=0,
        )
        self.assertTrue(any("network denied" in item for item in denied.reasons))


@mock.patch('agentflow.opencode_adapter._read_output_config', new=resolved_output_stub)
class RemoteWorkerAdapterTests(unittest.TestCase):
    def _adapter(self, tempdir: Path) -> RemoteOpenCodeWorkerAdapter:
        return RemoteOpenCodeWorkerAdapter(
            WORKER_PROVIDER,
            planned_models=(worker_request().model,),
            opencode_command="opencode-stub",
        )

    def test_command_array_and_worker_permissions(self) -> None:
        events = "\n".join(
            (
                json.dumps(
                    {
                        "type": "text",
                        "sessionID": "session-1",
                        "part": {"type": "text", "text": "implementation complete"},
                    }
                ),
                json.dumps(
                    {"type": "step_finish", "part": {"tokens": {"input": 5, "output": 2}}}
                ),
            )
        )
        discovery = subprocess.CompletedProcess(
            ("opencode",), 0, stdout=f"{WORKER_PROVIDER}/{WORKER_MODEL}\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            captured: dict[str, object] = {}

            def popen(command, **kwargs):
                captured["command"] = command
                captured["kwargs"] = kwargs
                return _CompletedProcess(events)

            adapter = self._adapter(worktree)
            request = replace(
                worker_request(),
                metadata={**worker_request().metadata, "worktree": str(worktree), "allowed_files": ("out.txt",)},
            )
            with mock.patch("subprocess.run", return_value=discovery), mock.patch(
                "subprocess.Popen", side_effect=popen
            ):
                result = adapter.invoke(request)

            command = captured["command"]
            self.assertIsInstance(command, tuple)
            self.assertEqual(
                f"{WORKER_PROVIDER}/{WORKER_MODEL}",
                command[command.index("--model") + 1],
            )
            self.assertEqual(
                "agentflow-remote-worker", command[command.index("--agent") + 1]
            )
            self.assertEqual(str(worktree.resolve()), command[command.index("--dir") + 1])
            config = json.loads(captured["kwargs"]["env"]["OPENCODE_CONFIG_CONTENT"])
            permission = config["permission"]
            for name in ("edit", "write", "glob", "grep"):
                self.assertEqual("allow", permission[name])
            for name in (
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
            self.assertEqual([WORKER_PROVIDER], config["enabled_providers"])
            self.assertEqual(5, result.input_tokens)
            self.assertEqual(2, result.output_tokens)

    def test_role_and_read_only_enforced_before_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(Path(directory))
            with mock.patch("subprocess.run") as discovery:
                with self.assertRaisesRegex(ValueError, "remote role denied"):
                    adapter.invoke(replace(worker_request(), role="review"))
                with self.assertRaisesRegex(ValueError, "remote role denied"):
                    adapter.invoke(replace(worker_request(), read_only=True))
            discovery.assert_not_called()

    def test_zero_exit_step_limit_is_incomplete_not_success(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        discovery = subprocess.CompletedProcess(
            ("opencode",), 0, stdout=f"{WORKER_PROVIDER}/{WORKER_MODEL}\n", stderr=""
        )
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(Path(directory))
            request = replace(worker_request(), metadata={**worker_request().metadata, "worktree": directory})
            with mock.patch("subprocess.run", return_value=discovery), mock.patch(
                "subprocess.Popen",
                return_value=_CompletedProcess(fixture.read_text(encoding="utf-8")),
            ):
                with self.assertRaises(InvocationIncompleteError):
                    adapter.invoke(request)

    def test_interrupted_worker_process_is_unknown(self) -> None:
        discovery = subprocess.CompletedProcess(
            ("opencode",), 0, stdout=f"{WORKER_PROVIDER}/{WORKER_MODEL}\n", stderr=""
        )

        class TimedOutProcess:
            pid = 51235
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(("opencode",), 1)
                return "", ""

        process = TimedOutProcess()
        with tempfile.TemporaryDirectory() as directory:
            adapter = self._adapter(Path(directory))
            request = replace(worker_request(), metadata={**worker_request().metadata, "worktree": directory})
            with mock.patch("subprocess.run", return_value=discovery), mock.patch(
                "subprocess.Popen", return_value=process
            ), mock.patch(
                "agentflow.opencode_adapter.OpenCodeAdapter.cancel", return_value=True
            ):
                with self.assertRaises(InvocationOutcomeUnknown) as caught:
                    adapter.invoke(request)
                self.assertIsNotNone(caught.exception.result)
                self.assertEqual("", caught.exception.result.output)
                self.assertTrue(caught.exception.result.cost_unavailable)


class RemoteWorkerRunnerTests(unittest.TestCase):
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

    def _implement(self, request: InvocationRequest) -> InvocationResult:
        path = Path(request.metadata["worktree"]) / request.metadata["allowed_files"][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"created by {request.role}\n", encoding="utf-8")
        return InvocationResult(
            f"fake:{request.request_key}", "implementation complete", 0, 0, 0, 0, 0,
            {"test_double": True},
        )

    def _approve(self, request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            f"fake:{request.request_key}",
            json.dumps({"approved": True, "findings": []}),
            0, 0, 0, 0, 0,
            {"test_double": True},
        )

    def test_remote_implementation_completes_end_to_end(self) -> None:
        task = worker_task()
        plan = worker_plan(task)
        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=self._approve),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="worker-run")
        self.assertEqual("completed", result.state.value)
        self.assertEqual(TaskState.APPROVED, self.database.task_state("worker-run", task.task_id))
        worktree = Path(self.database.run_snapshot("worker-run")["tasks"][0]["worktree_path"])
        self.assertTrue((worktree / task.expected_outputs[0]).is_file())

    def test_input_artifact_mismatch_fails_task(self) -> None:
        artifact = InputArtifact("seed.txt", "f" * 64)
        task = worker_task(input_artifacts=(artifact,))
        plan = worker_plan(task)
        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=self._approve),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="artifact-mismatch-run")
        self.assertEqual("failed", result.state.value)
        attempt = self.database.latest_attempt("artifact-mismatch-run", task.task_id)
        self.assertEqual("input_artifact_mismatch", attempt["outcome"])

    def test_input_artifact_match_allows_worker(self) -> None:
        digest = hashlib.sha256(b"seed\n").hexdigest()
        task = worker_task(input_artifacts=(InputArtifact("seed.txt", digest),))
        plan = worker_plan(task)
        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=self._approve),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="artifact-ok-run")
        self.assertEqual("completed", result.state.value)

    def test_untracked_input_artifact_completes(self) -> None:
        (self.root / "extra-input.txt").write_text("extra\n", encoding="utf-8")
        digest = hashlib.sha256(b"extra\n").hexdigest()
        task = worker_task(input_artifacts=(InputArtifact("extra-input.txt", digest),))
        plan = worker_plan(task)
        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=self._approve),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="untracked-input-run")
        self.assertEqual("completed", result.state.value)

    def test_zero_findings_policy_blocks_non_p0_p1_finding(self) -> None:
        task = worker_task(review_policy=ReviewAcceptancePolicy.ZERO_FINDINGS)
        plan = worker_plan(task)

        def review_with_p2(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                f"fake:{request.request_key}",
                json.dumps(
                    {
                        "approved": True,
                        "findings": [
                            {
                                "severity": "P2",
                                "title": "Minor wording",
                                "explanation": "A cosmetic issue.",
                            }
                        ],
                    }
                ),
                0, 0, 0, 0, 0,
                {"test_double": True},
            )

        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=review_with_p2),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="zero-findings-run")
        self.assertEqual("failed", result.state.value)

    def test_remote_worker_step_limit_pauses_without_continuation(self) -> None:
        task = worker_task()
        plan = worker_plan(task)

        def incomplete(_request: InvocationRequest) -> InvocationResult:
            fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
            from agentflow.opencode_adapter import parse_opencode_json

            return parse_opencode_json(
                fixture.read_text(encoding="utf-8"), duration_ms=1, is_local=False
            )

        worker = FakeAdapter(responder=incomplete)
        runner = Runner(
            self.database,
            AdapterRouter({"fake": FakeAdapter(responder=self._approve), WORKER_PROVIDER: worker}),
            self.workspace,
        )
        authorization = issue_authorization(plan)
        result = runner.start(plan, authorization, run_id="worker-step-limit-run")
        self.assertEqual("paused", result.state.value)
        checkpoint = json.loads(
            self.database.run_snapshot("worker-step-limit-run")["run"]["checkpoint_json"]
        )
        self.assertEqual("implementation_step_limit_reached", checkpoint["reason"])
        self.assertEqual(1, len(worker.invocations))

    def test_supervisor_wake_event_recorded_on_p0_p1_finding(self) -> None:
        task = worker_task()
        plan = worker_plan(
            task,
            supervisor_policy=SupervisorPolicy(wake_events=("p0_p1_finding",)),
        )

        def review_with_p1(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                f"fake:{request.request_key}",
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
                0, 0, 0, 0, 0,
                {"test_double": True},
            )

        Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=review_with_p1),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="wake-run")
        pending = self.database.pending_supervisor_checkpoints("wake-run")
        reasons = [item["reason"] for item in pending]
        self.assertEqual(["p0_p1_finding"], reasons)

    def test_mandatory_wake_event_recorded_even_when_not_configured(self) -> None:
        task = worker_task()

        def review_with_p1(request: InvocationRequest) -> InvocationResult:
            return InvocationResult(
                f"fake:{request.request_key}",
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
                0, 0, 0, 0, 0,
                {"test_double": True},
            )

        plan = worker_plan(task)
        Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=review_with_p1),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="mandatory-wake-run")
        pending = self.database.pending_supervisor_checkpoints("mandatory-wake-run")
        reasons = [item["reason"] for item in pending]
        self.assertEqual(["p0_p1_finding"], reasons)
        self.assertEqual(
            "paused",
            self.database.run_snapshot("mandatory-wake-run")["run"]["run_state"],
        )


class SupervisorStoreTests(unittest.TestCase):
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
        self.database.create_run("supervisor-run", plan, authorization)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_record_next_acknowledge_and_digest(self) -> None:
        checkpoint_id = self.database.record_supervisor_checkpoint(
            "supervisor-run",
            "p0_p1_finding",
            {"task_id": "remote-impl"},
            max_chars=6000,
            max_checkpoints=10,
        )
        self.assertIsNotNone(checkpoint_id)
        next_checkpoint = self.database.next_supervisor_checkpoint("supervisor-run")
        self.assertEqual("p0_p1_finding", next_checkpoint["reason"])
        self.assertEqual(
            {"task_id": "remote-impl"}, json.loads(next_checkpoint["content_json"])
        )
        plan_hash_value = self.database.supervisor_run_plan_hash("supervisor-run")
        cursor = self.database.latest_event_sequence("supervisor-run")
        self.database.acknowledge_supervisor_checkpoint(
            "supervisor-run",
            checkpoint_id,
            {"action": "escalate", "plan_hash": plan_hash_value, "cursor": cursor},
        )
        self.assertIsNone(self.database.next_supervisor_checkpoint("supervisor-run"))
        digest = self.database.supervisor_digest("supervisor-run")
        self.assertFalse(digest["wake_required"])
        self.assertEqual(plan_hash_value, digest["plan_hash"])
        self.assertIn("digest_sha256", digest)

    def test_bounded_content_truncates_large_payload(self) -> None:
        self.database.record_supervisor_checkpoint(
            "supervisor-run",
            "acceptance_unmet",
            {"blob": "x" * 10000},
            max_chars=200,
            max_checkpoints=10,
        )
        row = self.database.next_supervisor_checkpoint("supervisor-run")
        content = json.loads(row["content_json"])
        self.assertTrue(content["truncated"])
        self.assertLessEqual(len(row["content_json"].encode("utf-8")), 200)

    def test_bounded_json_respects_utf8_byte_limit(self) -> None:
        payload = {"blob": "你" * 5000}
        result = Database._bounded_supervisor_json(payload, max_chars=200)
        self.assertLessEqual(len(result.encode("utf-8")), 200)
        content = json.loads(result)
        self.assertTrue(content["truncated"])
        self.assertEqual(
            len(canonical_json(payload).encode("utf-8")), content["original_bytes"]
        )

    def test_bounded_digest_fits_within_limit_including_digest(self) -> None:
        digest = {
            "schema_version": 1,
            "run_id": "r",
            "plan_hash": "h" * 64,
            "authorization_id": "a",
            "changed": True,
            "wake_required": True,
            "recommended_reasoning_effort": "high",
            "reason_codes": ["p0_p1_finding"],
            "run_state": "running",
            "control_state": "running",
            "cursor": 5,
            "tasks": [{"objective": "x" * 2000}],
            "scope": ["src/a.py"] * 500,
            "cost": {"budget": 1},
            "unknown_call_count": 0,
        }
        max_chars = 1000
        bounded = Database._bounded_supervisor_digest(digest, max_chars)
        bounded["digest_sha256"] = digest_sha256(bounded)
        self.assertLessEqual(len(canonical_json(bounded).encode("utf-8")), max_chars)
        self.assertIn("reason_codes", bounded)
        self.assertIn("plan_hash", bounded)
        self.assertIn("cursor", bounded)


class SupervisorDigestHardCapTests(unittest.TestCase):
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
        self.plan = worker_plan(
            supervisor_policy=SupervisorPolicy(max_checkpoint_chars=1000)
        )
        authorization = replace(
            issue_authorization(self.plan), authorization_id="a" * 5000
        )
        self.database.save_plan(self.plan)
        self.database.save_authorization(authorization)
        self.long_run_id = "run-" + "x" * 5000
        self.database.create_run(self.long_run_id, self.plan, authorization)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_digest_fits_hard_cap_with_long_identity_fields(self) -> None:
        digest = self.database.supervisor_digest(self.long_run_id)
        max_chars = self.plan.supervisor_policy.max_checkpoint_chars
        self.assertLessEqual(len(canonical_json(digest).encode("utf-8")), max_chars)
        self.assertIn("digest_sha256", digest)
        self.assertTrue(digest["truncated"])
        self.assertIn("sha256", digest["run_id"])
        self.assertIn("plan_hash", digest)
        self.assertIn("reason_codes", digest)
        self.assertIn("run_state", digest)
        self.assertIn("control_state", digest)
        self.assertIn("cursor", digest)
        self.assertIn("original_bytes", digest)
        self.assertIn("wake_required", digest)
        self.assertIn("schema_version", digest)

    def test_bounded_digest_cjk_content_fits(self) -> None:
        digest = {
            "schema_version": 2,
            "run_id": "运行" * 2000,
            "plan_hash": "p" * 64,
            "wake_required": True,
            "reason_codes": ["隐私违规" * 300],
            "run_state": "paused",
            "control_state": "paused",
            "cursor": 7,
            "changed": True,
            "recommended_reasoning_effort": "high",
            "unknown_call_count": 0,
            "tasks": [{"objective": "你" * 3000}],
            "scope": [],
            "cost": {},
        }
        max_chars = 1000
        bounded = Database._bounded_supervisor_digest(digest, max_chars)
        bounded["digest_sha256"] = digest_sha256(bounded)
        self.assertLessEqual(len(canonical_json(bounded).encode("utf-8")), max_chars)
        self.assertIn("sha256", bounded["run_id"])

    def test_bounded_digest_bounds_long_reason_list(self) -> None:
        digest = {
            "schema_version": 2,
            "run_id": "r",
            "plan_hash": "p" * 64,
            "wake_required": True,
            "reason_codes": [f"reason-{i}" for i in range(2000)],
            "run_state": "paused",
            "control_state": "paused",
            "cursor": 5,
        }
        max_chars = 1000
        bounded = Database._bounded_supervisor_digest(digest, max_chars)
        bounded["digest_sha256"] = digest_sha256(bounded)
        self.assertLessEqual(len(canonical_json(bounded).encode("utf-8")), max_chars)
        self.assertIn("sha256", bounded["reason_codes"])


class SupervisorCliTests(unittest.TestCase):
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
        self.plan = worker_plan()
        (self.root / ".agentflow").mkdir(exist_ok=True)
        (self.root / ".agentflow" / "plan.json").write_text(
            canonical_json(self.plan), encoding="utf-8"
        )
        database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        database.initialize()
        authorization = issue_authorization(self.plan)
        database.save_plan(self.plan)
        database.save_authorization(authorization)
        database.create_run("sup-cli-run", self.plan, authorization)
        self.checkpoint_id = database.record_supervisor_checkpoint(
            "sup-cli-run",
            "p0_p1_finding",
            {"task_id": "remote-impl"},
            max_chars=6000,
            max_checkpoints=10,
        )
        database.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def call(self, *arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = main(["--project", str(self.root), *arguments])
        return result, stdout.getvalue(), stderr.getvalue()

    def test_supervisor_next_and_record(self) -> None:
        code, output, error = self.call("supervisor-next", "sup-cli-run")
        self.assertEqual(0, code, error)
        payload = json.loads(output)
        self.assertTrue(payload["wake_required"])
        self.assertIn("p0_p1_finding", payload["reason_codes"])
        cursor = payload["cursor"]

        decision = {
            "action": "pause",
            "plan_hash": plan_hash(self.plan),
            "cursor": cursor,
        }
        code, output, error = self.call(
            "supervisor-record", "sup-cli-run", self.checkpoint_id,
            "--decision", json.dumps(decision),
        )
        self.assertEqual(0, code, error)
        self.assertEqual("acknowledged", json.loads(output)["status"])

        code, output, error = self.call("supervisor-next", "sup-cli-run")
        self.assertEqual(0, code, error)
        self.assertEqual(False, json.loads(output)["wake_required"])


class RemoteWorkerCliWiringTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_runner_registers_worker_adapter_for_remote_implementation(self) -> None:
        from agentflow.cli import _runner
        from agentflow.config import resolve_paths

        paths = resolve_paths(self.root)
        database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        database.initialize()
        try:
            plan = worker_plan()
            authorization = issue_authorization(plan)
            worker = mock.Mock()
            worker.test_double = True
            worker.require_model = mock.Mock()
            worker.discover.return_value = []
            with mock.patch(
                "agentflow.cli.RemoteOpenCodeWorkerAdapter", return_value=worker
            ) as worker_cls, mock.patch(
                "agentflow.cli.RemoteOpenCodeReviewerAdapter"
            ) as reviewer_cls:
                runner = _runner(paths, database, plan, authorization)
            worker_cls.assert_called_once()
            reviewer_cls.assert_not_called()
            self.assertIs(worker, runner.adapter.adapter_for(WORKER_PROVIDER, "implementation"))
        finally:
            database.close()

    def test_provider_can_serve_both_roles_by_role(self) -> None:
        from agentflow.cli import _runner
        from agentflow.config import resolve_paths

        paths = resolve_paths(self.root)
        database = Database(self.root / ".agentflow" / "runs" / "agentflow.db")
        database.initialize()
        try:
            task = replace(
                worker_task(),
                review_model=ModelRef(
                    WORKER_PROVIDER, "reviewer-model", "1", "reviewer-family", False
                ),
            )
            plan = worker_plan(task)
            authorization = issue_authorization(plan)
            worker = mock.Mock()
            worker.test_double = True
            worker.require_model = mock.Mock()
            worker.discover.return_value = []
            reviewer = mock.Mock()
            reviewer.test_double = True
            reviewer.require_model = mock.Mock()
            reviewer.discover.return_value = []
            with mock.patch(
                "agentflow.cli.RemoteOpenCodeWorkerAdapter", return_value=worker
            ) as worker_cls, mock.patch(
                "agentflow.cli.RemoteOpenCodeReviewerAdapter", return_value=reviewer
            ) as reviewer_cls:
                runner = _runner(paths, database, plan, authorization)
            worker_cls.assert_called_once()
            reviewer_cls.assert_called_once()
            self.assertIs(
                worker, runner.adapter.adapter_for(WORKER_PROVIDER, "implementation")
            )
            self.assertIs(
                reviewer, runner.adapter.adapter_for(WORKER_PROVIDER, "review")
            )
        finally:
            database.close()


class StrictSerializationTests(unittest.TestCase):
    def test_allow_remote_implementation_rejects_string_bool(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["allow_remote_implementation"] = "false"
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_continuous_llm_monitoring_rejects_string_bool(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["supervisor_policy"]["continuous_llm_monitoring"] = "true"
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_max_supervisor_checkpoints_rejects_bool(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["supervisor_policy"]["max_supervisor_checkpoints"] = True
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_remote_worker_timeout_rejects_float(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["remote_worker_timeout_seconds"] = 30.5
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_input_artifact_sha256_rejects_non_string(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["input_artifacts"] = [{"path": "x.txt", "sha256": 123}]
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_remote_worker_allowed_hosts_rejects_string(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["remote_worker_allowed_hosts"] = "example.com"
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_remote_worker_allowed_hosts_rejects_non_string_items(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["remote_worker_allowed_hosts"] = [123]
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_input_artifacts_rejects_string(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["input_artifacts"] = "seed.txt"
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_input_artifacts_rejects_non_object_items(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["input_artifacts"] = [["seed.txt"]]
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_wake_events_rejects_string(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["supervisor_policy"]["wake_events"] = "p0_p1_finding"
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_wake_events_rejects_non_string_items(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["supervisor_policy"]["wake_events"] = [123]
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_remote_worker_allowed_hosts_rejects_explicit_null(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["remote_worker_allowed_hosts"] = None
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_wake_events_rejects_explicit_null(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["supervisor_policy"]["wake_events"] = None
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_input_artifacts_rejects_explicit_null(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["input_artifacts"] = None
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_input_artifacts_missing_path_raises_value_error(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["input_artifacts"] = [
            {"sha256": "0" * 64},
        ]
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_input_artifacts_missing_sha256_raises_value_error(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["input_artifacts"] = [
            {"path": "seed.txt"},
        ]
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))

    def test_input_artifacts_null_path_raises_value_error(self) -> None:
        data = json.loads(canonical_json(worker_plan()))
        data["tasks"][0]["input_artifacts"] = [
            {"path": None, "sha256": "0" * 64},
        ]
        with self.assertRaises(ValueError):
            load_plan_json(json.dumps(data))


class StagingSandboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", ".env", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        self.runs = self.root / ".agentflow" / "runs"
        self.workspace = GitWorkspace(self.root, self.runs)
        self.worktree = self.workspace.create("sandbox-run", "sandbox-task")
        self.artifact = InputArtifact(
            "seed.txt", hashlib.sha256(b"seed\n").hexdigest()
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _sandbox(self):
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="sandbox-run",
            task_id="sandbox-task",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(self.artifact,),
            allowed_files=("out.txt",),
            briefing="briefing",
        )
        return sandbox, baseline

    def test_sandbox_excludes_tracked_secret_and_git(self) -> None:
        sandbox, _ = self._sandbox()
        self.assertFalse((sandbox / ".env").exists())
        self.assertFalse((sandbox / ".git").exists())
        self.assertTrue((sandbox / "seed.txt").is_file())
        self.assertTrue((sandbox / ".agentflow-briefing.md").is_file())

    def test_extra_file_outside_scope_fails_all_or_nothing(self) -> None:
        sandbox, baseline = self._sandbox()
        (sandbox / "extra.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(StagingSyncError):
            self.workspace.sync_staging_outputs(
                sandbox,
                self.worktree,
                input_artifacts=(self.artifact,),
                allowed_files=("out.txt",),
                baseline=baseline,
            )
        self.assertFalse((self.worktree / "extra.txt").exists())
        self.assertFalse((self.worktree / "out.txt").exists())

    def test_worker_produced_symlink_is_rejected(self) -> None:
        sandbox, baseline = self._sandbox()
        (sandbox / "out.txt").symlink_to(sandbox / "seed.txt")
        with self.assertRaises(StagingSyncError):
            self.workspace.sync_staging_outputs(
                sandbox,
                self.worktree,
                input_artifacts=(self.artifact,),
                allowed_files=("out.txt",),
                baseline=baseline,
            )
        self.assertFalse((self.worktree / "out.txt").exists())

    def test_create_staging_sandbox_rejects_symlinked_parent(self) -> None:
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "pwn.txt").write_text("pwned\n", encoding="utf-8")
        (self.worktree / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StagingSyncError):
            self.workspace.create_staging_sandbox(
                run_id="sandbox-run",
                task_id="sandbox-task",
                attempt_id="attempt-1",
                worktree=self.worktree,
                input_artifacts=(),
                allowed_files=("escape/pwn.txt",),
                briefing="briefing",
            )

    def test_sync_rejects_symlinked_parent_in_worktree(self) -> None:
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="sandbox-run",
            task_id="sandbox-task",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(),
            allowed_files=("escape/pwn.txt",),
            briefing="briefing",
        )
        (sandbox / "escape").mkdir(parents=True, exist_ok=True)
        (sandbox / "escape" / "pwn.txt").write_text("x\n", encoding="utf-8")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(exist_ok=True)
        (self.worktree / "escape").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(StagingSyncError):
            self.workspace.sync_staging_outputs(
                sandbox,
                self.worktree,
                input_artifacts=(),
                allowed_files=("escape/pwn.txt",),
                baseline=baseline,
            )
        self.assertFalse((outside / "pwn.txt").exists())

    def test_worker_produced_symlinked_directory_is_rejected(self) -> None:
        sandbox, baseline = self._sandbox()
        (sandbox / "out.txt").mkdir()
        (sandbox / "linked").symlink_to(sandbox / "out.txt", target_is_directory=True)
        with self.assertRaises(StagingSyncError):
            self.workspace.sync_staging_outputs(
                sandbox,
                self.worktree,
                input_artifacts=(self.artifact,),
                allowed_files=("out.txt",),
                baseline=baseline,
            )

    def test_snapshot_rejects_internal_parent_symlink(self) -> None:
        (self.root / "real").mkdir()
        (self.root / "real" / "input.txt").write_text("secret\n", encoding="utf-8")
        (self.root / "link").symlink_to(self.root / "real", target_is_directory=True)
        artifact = InputArtifact(
            "link/input.txt", hashlib.sha256(b"secret\n").hexdigest()
        )
        with self.assertRaises(InputArtifactError):
            self.workspace.create_staging_sandbox(
                run_id="sandbox-run",
                task_id="sandbox-task",
                attempt_id="attempt-1",
                worktree=self.worktree,
                input_artifacts=(artifact,),
                allowed_files=("out.txt",),
                briefing="briefing",
            )

    def test_snapshot_rejects_missing_input(self) -> None:
        artifact = InputArtifact("nope.txt", "f" * 64)
        with self.assertRaises(InputArtifactError):
            self.workspace.create_staging_sandbox(
                run_id="sandbox-run",
                task_id="sandbox-task",
                attempt_id="attempt-1",
                worktree=self.worktree,
                input_artifacts=(artifact,),
                allowed_files=("out.txt",),
                briefing="briefing",
            )

    def test_sync_restores_executable_mode_on_rollback(self) -> None:
        (self.worktree / "b.txt").write_text("original-b\n", encoding="utf-8")
        (self.worktree / "b.txt").chmod(0o755)
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="sandbox-run",
            task_id="sandbox-task",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(),
            allowed_files=("a.txt", "b.txt"),
            briefing="briefing",
        )
        (sandbox / "a.txt").write_text("new-a\n", encoding="utf-8")
        (sandbox / "b.txt").write_text("new-b\n", encoding="utf-8")
        real = workspace_module._atomic_write_bytes

        def failing_write(destination: Path, content: bytes) -> None:
            if destination.name == "b.txt":
                raise OSError("injected failure")
            real(destination, content)

        with mock.patch(
            "agentflow.workspace._atomic_write_bytes", side_effect=failing_write
        ):
            with self.assertRaises(StagingSyncError):
                self.workspace.sync_staging_outputs(
                    sandbox,
                    self.worktree,
                    input_artifacts=(),
                    allowed_files=("a.txt", "b.txt"),
                    baseline=baseline,
                )
        self.assertFalse((self.worktree / "a.txt").exists())
        self.assertEqual(
            "original-b\n", (self.worktree / "b.txt").read_text(encoding="utf-8")
        )
        self.assertEqual(0o755, (self.worktree / "b.txt").stat().st_mode & 0o777)

    def test_sync_removes_new_nested_parent_dirs_on_rollback(self) -> None:
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="sandbox-run",
            task_id="sandbox-task",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(),
            allowed_files=("nested/deep/a.txt", "nested/deep/b.txt"),
            briefing="briefing",
        )
        (sandbox / "nested" / "deep").mkdir(parents=True, exist_ok=True)
        (sandbox / "nested" / "deep" / "a.txt").write_text("a\n", encoding="utf-8")
        (sandbox / "nested" / "deep" / "b.txt").write_text("b\n", encoding="utf-8")
        real = workspace_module._atomic_write_bytes

        def failing_write(destination: Path, content: bytes) -> None:
            if destination.name == "b.txt":
                raise OSError("injected failure")
            real(destination, content)

        with mock.patch(
            "agentflow.workspace._atomic_write_bytes", side_effect=failing_write
        ):
            with self.assertRaises(StagingSyncError):
                self.workspace.sync_staging_outputs(
                    sandbox,
                    self.worktree,
                    input_artifacts=(),
                    allowed_files=("nested/deep/a.txt", "nested/deep/b.txt"),
                    baseline=baseline,
                )
        self.assertFalse((self.worktree / "nested" / "deep" / "a.txt").exists())
        self.assertFalse((self.worktree / "nested" / "deep" / "b.txt").exists())
        self.assertFalse((self.worktree / "nested").exists())

    def test_sync_rolls_back_when_a_later_write_fails(self) -> None:
        (self.worktree / "b.txt").write_text("original-b\n", encoding="utf-8")
        sandbox, _, baseline = self.workspace.create_staging_sandbox(
            run_id="sandbox-run",
            task_id="sandbox-task",
            attempt_id="attempt-1",
            worktree=self.worktree,
            input_artifacts=(),
            allowed_files=("a.txt", "b.txt"),
            briefing="briefing",
        )
        (sandbox / "a.txt").write_text("new-a\n", encoding="utf-8")
        (sandbox / "b.txt").write_text("new-b\n", encoding="utf-8")
        real = workspace_module._atomic_write_bytes

        def failing_write(destination: Path, content: bytes) -> None:
            if destination.name == "b.txt":
                raise OSError("injected failure")
            real(destination, content)

        with mock.patch(
            "agentflow.workspace._atomic_write_bytes", side_effect=failing_write
        ):
            with self.assertRaises(StagingSyncError):
                self.workspace.sync_staging_outputs(
                    sandbox,
                    self.worktree,
                    input_artifacts=(),
                    allowed_files=("a.txt", "b.txt"),
                    baseline=baseline,
                )
        self.assertFalse((self.worktree / "a.txt").exists())
        self.assertEqual(
            "original-b\n", (self.worktree / "b.txt").read_text(encoding="utf-8")
        )


class SupervisorProtocolTests(unittest.TestCase):
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

    def _checkpoint(self) -> str:
        return self.database.record_supervisor_checkpoint(
            "protocol-run",
            "p0_p1_finding",
            {"task_id": "remote-impl"},
            max_chars=6000,
            max_checkpoints=10,
        )

    def _plan_hash(self) -> str:
        return self.database.supervisor_run_plan_hash("protocol-run")

    def test_terminal_checkpoint_is_idempotent(self) -> None:
        first = self.database.record_terminal_supervisor_checkpoint(
            "protocol-run", "run_completed", {}, max_chars=6000, max_checkpoints=10
        )
        second = self.database.record_terminal_supervisor_checkpoint(
            "protocol-run", "run_completed", {}, max_chars=6000, max_checkpoints=10
        )
        self.assertEqual(first, second)

    def test_finalize_run_sets_state_and_terminal_checkpoint_together(self) -> None:
        self.database.finalize_run(
            "protocol-run",
            RunState.FAILED,
            "run_failed",
            {},
            max_chars=6000,
            max_checkpoints=10,
        )
        run = self.database.run_snapshot("protocol-run")["run"]
        self.assertEqual(RunState.FAILED.value, run["run_state"])
        self.assertIsNotNone(run["finished_at"])
        terminal = [
            row
            for row in self.database.pending_supervisor_checkpoints("protocol-run")
            if row["terminal"]
        ]
        self.assertEqual(1, len(terminal))
        self.assertEqual("run_failed", terminal[0]["reason"])

    def test_finalize_run_is_idempotent_for_terminal_checkpoint(self) -> None:
        first = self.database.finalize_run(
            "protocol-run", RunState.FAILED, "run_failed", {},
            max_chars=6000, max_checkpoints=10,
        )
        second = self.database.finalize_run(
            "protocol-run", RunState.FAILED, "run_failed", {},
            max_chars=6000, max_checkpoints=10,
        )
        self.assertEqual(first, second)

    def test_finalize_run_rejects_non_terminal_state(self) -> None:
        with self.assertRaises(ValueError):
            self.database.finalize_run(
                "protocol-run", RunState.RUNNING, "run_failed", {},
                max_chars=6000, max_checkpoints=10,
            )
        with self.assertRaises(ValueError):
            self.database.finalize_run(
                "protocol-run", RunState.PAUSED, "run_failed", {},
                max_chars=6000, max_checkpoints=10,
            )

    def test_finalize_run_fails_closed_on_conflicting_state(self) -> None:
        self.database.finalize_run(
            "protocol-run", RunState.FAILED, "run_failed", {},
            max_chars=6000, max_checkpoints=10,
        )
        with self.assertRaises(RuntimeError):
            self.database.finalize_run(
                "protocol-run", RunState.COMPLETED, "run_completed", {},
                max_chars=6000, max_checkpoints=10,
            )
        run = self.database.run_snapshot("protocol-run")["run"]
        self.assertEqual(RunState.FAILED.value, run["run_state"])
        terminal = [
            row
            for row in self.database.pending_supervisor_checkpoints("protocol-run")
            if row["terminal"]
        ]
        self.assertEqual(1, len(terminal))
        self.assertEqual("run_failed", terminal[0]["reason"])

    def test_finalize_run_fails_closed_on_conflicting_content(self) -> None:
        self.database.finalize_run(
            "protocol-run", RunState.FAILED, "run_failed", {"cause": "a"},
            max_chars=6000, max_checkpoints=10,
        )
        with self.assertRaises(RuntimeError):
            self.database.finalize_run(
                "protocol-run", RunState.FAILED, "run_failed", {"cause": "b"},
                max_chars=6000, max_checkpoints=10,
            )
        run = self.database.run_snapshot("protocol-run")["run"]
        self.assertEqual(RunState.FAILED.value, run["run_state"])

    def test_finalize_run_fails_closed_on_conflicting_plan_hash(self) -> None:
        self.database.finalize_run(
            "protocol-run", RunState.FAILED, "run_failed", {},
            max_chars=6000, max_checkpoints=10, plan_hash_value="0" * 64,
        )
        with self.assertRaises(RuntimeError):
            self.database.finalize_run(
                "protocol-run", RunState.FAILED, "run_failed", {},
                max_chars=6000, max_checkpoints=10, plan_hash_value="1" * 64,
            )

    def test_checkpoint_limit_records_bounded_sentinel(self) -> None:
        for _ in range(3):
            self.database.record_supervisor_checkpoint(
                "protocol-run",
                "p0_p1_finding",
                {},
                max_chars=6000,
                max_checkpoints=3,
            )
        sentinel_id = self.database.record_supervisor_checkpoint(
            "protocol-run",
            "unknown_call",
            {"task_id": "task-1"},
            max_chars=6000,
            max_checkpoints=3,
        )
        pending = self.database.pending_supervisor_checkpoints("protocol-run")
        self.assertEqual(4, len(pending))
        sentinels = [
            checkpoint
            for checkpoint in pending
            if checkpoint["reason"] == "checkpoint_limit_reached"
        ]
        self.assertEqual(1, len(sentinels))
        self.assertEqual(sentinel_id, sentinels[0]["checkpoint_id"])
        content = json.loads(sentinels[0]["content_json"])
        self.assertEqual("unknown_call", content["dropped_reason"])
        self.assertEqual(3, content["checkpoint_limit"])

    def test_checkpoint_limit_sentinel_is_refreshed_not_duplicated(self) -> None:
        for _ in range(2):
            self.database.record_supervisor_checkpoint(
                "protocol-run",
                "p0_p1_finding",
                {},
                max_chars=6000,
                max_checkpoints=2,
            )
        first = self.database.record_supervisor_checkpoint(
            "protocol-run",
            "timeout",
            {},
            max_chars=6000,
            max_checkpoints=2,
        )
        second = self.database.record_supervisor_checkpoint(
            "protocol-run",
            "scope_violation",
            {},
            max_chars=6000,
            max_checkpoints=2,
        )
        self.assertEqual(first, second)
        pending = self.database.pending_supervisor_checkpoints("protocol-run")
        sentinels = [
            checkpoint
            for checkpoint in pending
            if checkpoint["reason"] == "checkpoint_limit_reached"
        ]
        self.assertEqual(1, len(sentinels))
        self.assertEqual("scope_violation", json.loads(sentinels[0]["content_json"])["dropped_reason"])
        events = [row["event_type"] for row in self.database.event_rows("protocol-run")]
        self.assertIn("supervisor.checkpoint_limit_reached", events)
        self.assertIn("supervisor.checkpoint_sentinel_refreshed", events)

    def test_checkpoint_limit_event_persists_despite_sentinel(self) -> None:
        for _ in range(2):
            self.database.record_supervisor_checkpoint(
                "protocol-run",
                "p0_p1_finding",
                {},
                max_chars=6000,
                max_checkpoints=2,
            )
        self.database.record_supervisor_checkpoint(
            "protocol-run",
            "p0_p1_finding",
            {},
            max_chars=6000,
            max_checkpoints=2,
        )
        events = [row["event_type"] for row in self.database.event_rows("protocol-run")]
        self.assertIn("supervisor.checkpoint_limit_reached", events)
        pending = self.database.pending_supervisor_checkpoints("protocol-run")
        self.assertEqual(
            3,  # two regular checkpoints plus one bounded sentinel
            len(pending),
        )

    def test_limit_all_acknowledged_then_unknown_records_sentinel(self) -> None:
        checkpoint_id = self.database.record_supervisor_checkpoint(
            "protocol-run", "unknown_call", {}, max_chars=6000, max_checkpoints=1
        )
        pending = self.database.pending_supervisor_checkpoints("protocol-run")
        self.assertEqual(1, len(pending))
        self.assertEqual("unknown_call", pending[0]["reason"])
        plan_hash_value = self._plan_hash()
        cursor = self.database.latest_event_sequence("protocol-run")
        self.database.acknowledge_supervisor_checkpoint(
            "protocol-run",
            checkpoint_id,
            {"action": "pause", "plan_hash": plan_hash_value, "cursor": cursor},
        )
        self.assertEqual(
            [], self.database.pending_supervisor_checkpoints("protocol-run")
        )
        sentinel_id = self.database.record_supervisor_checkpoint(
            "protocol-run", "unknown_call", {}, max_chars=6000, max_checkpoints=1
        )
        pending = self.database.pending_supervisor_checkpoints("protocol-run")
        self.assertEqual(1, len(pending))
        self.assertEqual("checkpoint_limit_reached", pending[0]["reason"])
        self.assertEqual(sentinel_id, pending[0]["checkpoint_id"])
        self.assertEqual(
            "unknown_call", json.loads(pending[0]["content_json"])["dropped_reason"]
        )

    def test_record_rejects_stale_cursor(self) -> None:
        checkpoint_id = self._checkpoint()
        with self.assertRaises(ValueError):
            self.database.acknowledge_supervisor_checkpoint(
                "protocol-run",
                checkpoint_id,
                {"action": "pause", "plan_hash": self._plan_hash(), "cursor": -1},
            )

    def test_record_rejects_wrong_plan_hash(self) -> None:
        checkpoint_id = self._checkpoint()
        cursor = self.database.latest_event_sequence("protocol-run")
        with self.assertRaises(ValueError):
            self.database.acknowledge_supervisor_checkpoint(
                "protocol-run",
                checkpoint_id,
                {"action": "pause", "plan_hash": "0" * 64, "cursor": cursor},
            )

    def test_record_is_idempotent_for_same_decision(self) -> None:
        checkpoint_id = self._checkpoint()
        cursor = self.database.latest_event_sequence("protocol-run")
        decision = {"action": "pause", "plan_hash": self._plan_hash(), "cursor": cursor}
        self.database.acknowledge_supervisor_checkpoint(
            "protocol-run", checkpoint_id, decision
        )
        self.database.acknowledge_supervisor_checkpoint(
            "protocol-run", checkpoint_id, decision
        )

    def test_record_rejects_conflicting_decision(self) -> None:
        checkpoint_id = self._checkpoint()
        cursor = self.database.latest_event_sequence("protocol-run")
        self.database.acknowledge_supervisor_checkpoint(
            "protocol-run",
            checkpoint_id,
            {"action": "pause", "plan_hash": self._plan_hash(), "cursor": cursor},
        )
        with self.assertRaises(RuntimeError):
            self.database.acknowledge_supervisor_checkpoint(
                "protocol-run",
                checkpoint_id,
                {"action": "resume", "plan_hash": self._plan_hash(), "cursor": cursor},
            )

    def test_digest_truncation_preserves_essential_fields(self) -> None:
        digest = {
            "schema_version": 1,
            "run_id": "r",
            "plan_hash": "h",
            "authorization_id": "a",
            "changed": True,
            "wake_required": True,
            "recommended_reasoning_effort": "high",
            "reason_codes": ["p0_p1_finding"],
            "run_state": "running",
            "control_state": "running",
            "cursor": 5,
            "tasks": [],
            "scope": [],
            "cost": {},
            "unknown_call_count": 0,
            "blob": "x" * 5000,
        }
        bounded = Database._bounded_supervisor_digest(digest, max_chars=200)
        self.assertTrue(bounded["truncated"])
        self.assertIn("plan_hash", bounded)
        self.assertIn("reason_codes", bounded)
        self.assertIn("cursor", bounded)
        self.assertNotIn("blob", bounded)
        self.assertEqual(
            len(canonical_json(digest).encode("utf-8")), bounded["original_bytes"]
        )


class InputArtifactSnapshotRunnerTests(unittest.TestCase):
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

    def _implement(self, request: InvocationRequest) -> InvocationResult:
        path = Path(request.metadata["worktree"]) / request.metadata["allowed_files"][0]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("created\n", encoding="utf-8")
        return InvocationResult(
            f"fake:{request.request_key}", "implementation complete", 0, 0, 0, 0, 0,
            {"test_double": True},
        )

    def _approve(self, request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            f"fake:{request.request_key}",
            json.dumps({"approved": True, "findings": []}),
            0, 0, 0, 0, 0,
            {"test_double": True},
        )

    def test_input_artifact_snapshot_event_recorded(self) -> None:
        digest = hashlib.sha256(b"seed\n").hexdigest()
        task = worker_task(input_artifacts=(InputArtifact("seed.txt", digest),))
        plan = worker_plan(task)
        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=self._approve),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="snapshot-event-run")
        self.assertEqual("completed", result.state.value)
        events = [
            row["event_type"] for row in self.database.event_rows("snapshot-event-run")
        ]
        self.assertIn("input_artifact.snapshotted", events)

    def test_input_artifact_manifest_persisted(self) -> None:
        digest = hashlib.sha256(b"seed\n").hexdigest()
        task = worker_task(input_artifacts=(InputArtifact("seed.txt", digest),))
        plan = worker_plan(task)
        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=self._approve),
                    WORKER_PROVIDER: FakeAdapter(responder=self._implement),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="manifest-run")
        self.assertEqual("completed", result.state.value)
        attempt = self.database.latest_attempt("manifest-run", task.task_id)
        manifest = self.database.input_artifact_manifest(
            "manifest-run", task.task_id, attempt["attempt_id"]
        )
        self.assertEqual(1, len(manifest))
        self.assertEqual("seed.txt", manifest[0]["path"])
        self.assertEqual(digest, manifest[0]["sha256"])
        self.assertEqual(5, manifest[0]["size"])
        self.assertEqual(attempt["attempt_id"], manifest[0]["attempt_id"])
        self.assertFalse(str(manifest[0]["path"]).startswith("/"))

    def test_worker_tampered_input_fails_before_sync(self) -> None:
        digest = hashlib.sha256(b"seed\n").hexdigest()
        task = worker_task(input_artifacts=(InputArtifact("seed.txt", digest),))
        plan = worker_plan(task)

        def implement_then_tamper(request: InvocationRequest) -> InvocationResult:
            path = Path(request.metadata["worktree"]) / request.metadata["allowed_files"][0]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("created\n", encoding="utf-8")
            sandbox = Path(request.metadata["worktree"])
            artifact = sandbox / "seed.txt"
            artifact.chmod(0o644)
            artifact.write_text("tampered\n", encoding="utf-8")
            return InvocationResult(
                f"fake:{request.request_key}", "done", 0, 0, 0, 0, 0,
                {"test_double": True},
            )

        result = Runner(
            self.database,
            AdapterRouter(
                {
                    "fake": FakeAdapter(responder=self._approve),
                    WORKER_PROVIDER: FakeAdapter(responder=implement_then_tamper),
                }
            ),
            self.workspace,
        ).start(plan, issue_authorization(plan), run_id="tampered-input-run")
        self.assertEqual("failed", result.state.value)
        attempt = self.database.latest_attempt("tampered-input-run", task.task_id)
        self.assertEqual("staging_sync_failed", attempt["outcome"])


class ReadOnlyMonitoringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, "init", "-b", "main")
        git(self.root, "config", "user.email", "tests@example.invalid")
        git(self.root, "config", "user.name", "AgentFlow Tests")
        (self.root / "seed.txt").write_text("seed\n", encoding="utf-8")
        git(self.root, "add", "seed.txt")
        git(self.root, "commit", "-m", "seed")
        self.db_path = self.root / ".agentflow" / "runs" / "agentflow.db"
        self.database = Database(self.db_path)
        self.database.initialize()
        plan = worker_plan()
        authorization = issue_authorization(plan)
        self.database.save_plan(plan)
        self.database.save_authorization(authorization)
        self.database.create_run("monitor-run", plan, authorization)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def test_latest_event_sequence_does_not_block_on_write_lock(self) -> None:
        self.database.connection.execute("BEGIN IMMEDIATE")
        monitor = Database(self.db_path)
        try:
            monitor.connection.execute("PRAGMA busy_timeout = 500")
            cursor = monitor.latest_event_sequence("monitor-run")
            self.assertIsInstance(cursor, int)
        finally:
            monitor.close()
            self.database.connection.execute("ROLLBACK")

    def test_event_sequence_count_matches_cursor_event_set(self) -> None:
        self.database.create_attempt("a1", "monitor-run", "remote-impl", 1)
        self.database.record_test(
            "t1",
            "monitor-run",
            "remote-impl",
            "a1",
            source="deterministic",
            passed=True,
            duration_ms=0,
            evidence={},
        )
        self.database.record_review(
            "r1",
            "monitor-run",
            "remote-impl",
            "a1",
            provider="fake",
            model_id="reviewer",
            model_version="1",
            approved=True,
            findings=[],
        )
        cursor = self.database.latest_event_sequence("monitor-run")
        count = self.database._event_sequence_count("monitor-run", 0, cursor)
        rows = self.database.event_rows("monitor-run", 0)
        self.assertGreater(count, 0)
        self.assertEqual(len(rows), count)


if __name__ == "__main__":
    unittest.main()
