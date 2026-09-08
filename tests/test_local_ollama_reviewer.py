"""Local Ollama packet-only read-only reviewer regression tests.

These exercise the local, loopback-only Ollama reviewer (``LocalOllamaReviewerAdapter``),
its strict endpoint parsing, its resolved-configuration binding, and the
routing/fallback/CLI wiring that keeps it review-only. They use no network, no
real model and no fees: ``opencode debug config --pure``, ``opencode models`` and
``opencode run`` are stubbed through ``subprocess`` mocks, and integration tests
drive the real Runner with a fake implementation model.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import unittest
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
    ReviewerUnavailableError,
    UnsupportedProviderError,
)
from agentflow.authorization import issue_authorization
from agentflow.cli import _runner
from agentflow.config import resolve_paths
from agentflow.contracts import (
    BusinessImportance,
    DataSensitivity,
    InvocationRequest,
    ModelAvailabilityState,
    ModelRef,
    OperationalSafety,
    RiskLevel,
)
from agentflow.database import Database
from agentflow.fake_adapter import FakeAdapter
from agentflow.opencode_adapter import (
    REMOTE_REVIEWER_MAX_STEPS,
    LocalOllamaReviewerAdapter,
    ollama_host_is_loopback,
    parse_opencode_failed_usage,
    parse_opencode_json,
    parse_opencode_partial_usage,
)
from agentflow.runner import Runner
from agentflow.states import InvocationState, RunState
from agentflow.workspace import GitWorkspace

from test_runner import approved_response, make_plan, make_task


PROVIDER = "ollama"
MODEL_ID = "local-model"

DENY_TOOLS = (
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
)


def git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)


def ollama_model(model_id: str = MODEL_ID, version: str = "1", family: str = "ollama-family") -> ModelRef:
    return ModelRef(PROVIDER, model_id, version, family, True)


def ollama_review_request(
    *,
    role: str = "review",
    read_only: bool = True,
    model_id: str = MODEL_ID,
) -> InvocationRequest:
    return InvocationRequest(
        call_id="call-1",
        request_key="request-1",
        run_id="run-1",
        task_id="ollama-review",
        role=role,
        model=ollama_model(model_id),
        prompt='{"task_id":"ollama-review"}',
        data_sensitivity=DataSensitivity.PUBLIC,
        read_only=read_only,
    )


def deny_permission() -> dict[str, str]:
    result: dict[str, str] = {"*": "deny"}
    result.update({tool: "deny" for tool in DENY_TOOLS})
    return result


def resolved_config_json(
    *,
    base_url: str = "http://127.0.0.1:11434/v1",
    model_id: str = MODEL_ID,
    model_extra: dict | None = None,
    npm: str = "@ai-sdk/openai-compatible",
    permission: dict | None = None,
    agent: dict | None = None,
    enabled_providers: list | None = None,
) -> str:
    cfg = {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": ["ollama"] if enabled_providers is None else enabled_providers,
        "provider": {
            "ollama": {
                "npm": npm,
                "options": {"baseURL": base_url},
                "models": {model_id: model_extra if model_extra is not None else {"name": "local model"}},
            }
        },
        "permission": permission if permission is not None else deny_permission(),
        "agent": agent
        if agent is not None
        else {
            "agentflow-local-ollama-reviewer": {
                "steps": REMOTE_REVIEWER_MAX_STEPS,
                "permission": deny_permission(),
            }
        },
    }
    return json.dumps(cfg)


def make_run_stub(
    *,
    base_url: str = "http://127.0.0.1:11434/v1",
    model_ids: tuple[str, ...] = (MODEL_ID,),
    model_id: str = MODEL_ID,
    model_extra: dict | None = None,
    npm: str = "@ai-sdk/openai-compatible",
    config: str | None = None,
    second_config: str | None = None,
):
    """Return a ``subprocess.run`` side effect that dispatches OpenCode commands.

    ``debug`` returns the base resolved config (or ``config``); when the call
    carries an injected ``OPENCODE_CONFIG_CONTENT`` env it returns that content
    (or ``second_config``, to simulate a managed override). ``models`` returns
    the configured model list.
    """

    def base_config() -> str:
        if config is not None:
            return config
        return resolved_config_json(
            base_url=base_url, model_id=model_id, model_extra=model_extra, npm=npm
        )

    def stub(args, **kwargs):
        if args[1] == "debug":
            env = kwargs.get("env")
            if env is not None and "OPENCODE_CONFIG_CONTENT" in env:
                if second_config is not None:
                    return subprocess.CompletedProcess(args, 0, stdout=second_config, stderr="")
                return subprocess.CompletedProcess(args, 0, stdout=env["OPENCODE_CONFIG_CONTENT"], stderr="")
            return subprocess.CompletedProcess(args, 0, stdout=base_config(), stderr="")
        if args[1] == "models":
            return subprocess.CompletedProcess(
                args, 0, stdout="".join(f"{PROVIDER}/{mid}\n" for mid in model_ids), stderr=""
            )
        raise AssertionError(f"unexpected subprocess.run args: {args!r}")

    return stub


def review_text_event(session: str = "ollama-session-1", approved: bool = True) -> str:
    return json.dumps(
        {
            "type": "text",
            "sessionID": session,
            "part": {
                "type": "text",
                "text": json.dumps({"approved": approved, "findings": []}),
            },
        }
    )


def step_finish_event(input_tokens: int = 7, output_tokens: int = 3) -> str:
    return json.dumps(
        {"type": "step_finish", "part": {"tokens": {"input": input_tokens, "output": output_tokens}}}
    )


def review_events(
    approved: bool = True, *, with_tokens: bool = False, session: str = "ollama-session-1"
) -> str:
    lines = [review_text_event(session, approved)]
    if with_tokens:
        lines.append(step_finish_event())
    return "\n".join(lines)


class _CompletedProcess:
    _next_pid = 40000

    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode
        _CompletedProcess._next_pid += 1
        self.pid = _CompletedProcess._next_pid

    def communicate(self, timeout=None):
        return self.stdout, ""


def _completed_process(stdout: str, returncode: int = 0) -> _CompletedProcess:
    return _CompletedProcess(stdout, returncode)


class OllamaLoopbackTests(unittest.TestCase):
    """Deterministic loopback detection for the local Ollama endpoint (O01)."""

    def test_loopback_boundary(self) -> None:
        positives = (
            None,
            "",
            "localhost",
            "LOCALHOST:11434",
            "127.255.255.254",
            "::1",
            "0:0:0:0:0:0:0:1",
            "[::1]:11434",
            "https://[::1]:11434/v1",
            "http://127.0.0.1:11434/v1/api",
        )
        negatives = (
            True,
            127,
            [],
            "127.999.1.1",
            "127.evil.example.com",
            "localhost.evil.example",
            "localhost.",
            "0.0.0.0",
            "192.168.1.5",
            "[::ffff:127.0.0.1]:11434",
            "[::1",
            "ftp://localhost",
            "http://localhost:abc",
            "http://localhost:0",
            "http://localhost:65536",
            "http://localhost:/v1",
            "http://localhost/?",
            "http://localhost/#",
            "http://localhost@evil.example",
            "http://user:pass@localhost",
            "http://localhost/?x=1",
            "http://localhost/#x",
        )
        for value in positives:
            with self.subTest(value=value):
                self.assertTrue(ollama_host_is_loopback(value))
        for value in negatives:
            with self.subTest(value=value):
                self.assertFalse(ollama_host_is_loopback(value))

    def test_determination_is_lexical_and_never_resolves_dns(self) -> None:
        with mock.patch.object(
            socket, "getaddrinfo", side_effect=AssertionError("DNS resolution attempted")
        ), mock.patch.object(
            socket, "gethostbyname", side_effect=AssertionError("DNS resolution attempted")
        ):
            self.assertTrue(ollama_host_is_loopback("localhost"))
            self.assertTrue(ollama_host_is_loopback("[::1]:11434"))
            self.assertTrue(ollama_host_is_loopback("http://127.0.0.1:11434"))
            self.assertFalse(ollama_host_is_loopback("http://some.internal:11434"))


class LocalOllamaConfigBindingTests(unittest.TestCase):
    """Resolved-configuration endpoint binding and fail-closed behaviour (O02-O07)."""

    def setUp(self) -> None:
        self.model = ollama_model()

    def _adapter(self, **kwargs) -> LocalOllamaReviewerAdapter:
        return LocalOllamaReviewerAdapter(
            planned_models=(self.model,),
            opencode_command="opencode-stub",
            **kwargs,
        )

    def test_remote_resolved_provider_endpoint_is_rejected_without_inference(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        with mock.patch("subprocess.run", side_effect=make_run_stub(base_url="http://10.0.0.5:11434/v1")) as run, mock.patch(
            "subprocess.Popen"
        ) as popen:
            with self.assertRaises(ProviderNotConfiguredError):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_missing_provider_and_missing_base_url_are_rejected(self) -> None:
        for config in (
            "{}",
            '{"provider": {}}',
            '{"provider": {"ollama": {"npm": "@ai-sdk/openai-compatible"}}}',
            '{"provider": {"ollama": {"npm": "@ai-sdk/openai-compatible", "options": {}}}}',
            '{"provider": {"ollama": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": ""}}}}',
            '{"provider": {"ollama": {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "   "}}}}',
        ):
            with self.subTest(config=config):
                adapter = self._adapter(ollama_host="127.0.0.1:11434")
                with mock.patch("subprocess.run", side_effect=make_run_stub(config=config)), mock.patch(
                    "subprocess.Popen"
                ) as popen:
                    with self.assertRaises(ProviderNotConfiguredError):
                        adapter.invoke(ollama_review_request())
                popen.assert_not_called()

    def test_explicit_host_conflicting_with_resolved_endpoint_is_rejected(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:9999")
        with mock.patch("subprocess.run", side_effect=make_run_stub(base_url="http://127.0.0.1:11434/v1")), mock.patch(
            "subprocess.Popen"
        ) as popen:
            with self.assertRaisesRegex(ProviderNotConfiguredError, "conflicts"):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_model_endpoint_override_is_rejected(self) -> None:
        for model_extra in (
            {"options": {"baseURL": "http://10.0.0.5:11434/v1"}},
            {"api": {"url": "http://10.0.0.5:11434/v1"}},
        ):
            with self.subTest(model_extra=model_extra):
                adapter = self._adapter(ollama_host="127.0.0.1:11434")
                with mock.patch(
                    "subprocess.run", side_effect=make_run_stub(model_extra=model_extra)
                ), mock.patch("subprocess.Popen") as popen:
                    with self.assertRaises(ProviderNotConfiguredError):
                        adapter.invoke(ollama_review_request())
                popen.assert_not_called()

    def test_unknown_transport_is_rejected(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        with mock.patch("subprocess.run", side_effect=make_run_stub(npm="not-a-transport")), mock.patch(
            "subprocess.Popen"
        ) as popen:
            with self.assertRaisesRegex(ProviderNotConfiguredError, "transport"):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_invalid_config_json_is_rejected(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        with mock.patch("subprocess.run", side_effect=make_run_stub(config="not json")), mock.patch(
            "subprocess.Popen"
        ) as popen:
            with self.assertRaisesRegex(ProviderNotConfiguredError, "not valid JSON"):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_config_discovery_timeout_is_controlled_error(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434", discovery_timeout_seconds=1)

        def timeout_stub(args, **kwargs):
            if args[1] == "debug":
                raise subprocess.TimeoutExpired(args, 1)
            raise AssertionError(f"unexpected args {args!r}")

        with mock.patch("subprocess.run", side_effect=timeout_stub), mock.patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(ProviderNotConfiguredError, "timed out"):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_missing_executable_is_unsupported_provider(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")

        def missing_stub(args, **kwargs):
            raise FileNotFoundError("opencode-stub")

        with mock.patch("subprocess.run", side_effect=missing_stub), mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(UnsupportedProviderError):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_config_discovery_permission_error_is_unsupported_provider(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")

        def permission_stub(args, **kwargs):
            raise PermissionError("opencode-stub not executable")

        with mock.patch("subprocess.run", side_effect=permission_stub), mock.patch(
            "subprocess.Popen"
        ) as popen:
            with self.assertRaises(UnsupportedProviderError):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_managed_override_back_to_remote_is_rejected_on_recheck(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        remote = resolved_config_json(base_url="http://10.0.0.5:11434/v1")
        with mock.patch(
            "subprocess.run", side_effect=make_run_stub(second_config=remote)
        ), mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(ProviderNotConfiguredError):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_managed_override_readding_allow_permission_is_rejected(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        allow = resolved_config_json(permission={**deny_permission(), "edit": "allow"})
        with mock.patch(
            "subprocess.run", side_effect=make_run_stub(second_config=allow)
        ), mock.patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(ProviderNotConfiguredError, "edit"):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_extra_tool_allow_in_global_permission_is_rejected(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        second = resolved_config_json(permission={**deny_permission(), "lsp": "allow"})
        with mock.patch(
            "subprocess.run", side_effect=make_run_stub(second_config=second)
        ), mock.patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(ProviderNotConfiguredError, "lsp"):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_extra_tool_allow_in_agent_permission_is_rejected(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        agent = {
            "agentflow-local-ollama-reviewer": {
                "steps": REMOTE_REVIEWER_MAX_STEPS,
                "permission": {**deny_permission(), "lsp": "allow"},
            }
        }
        second = resolved_config_json(agent=agent)
        with mock.patch(
            "subprocess.run", side_effect=make_run_stub(second_config=second)
        ), mock.patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(ProviderNotConfiguredError, "lsp"):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_ask_or_nested_permission_value_is_rejected(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        second = resolved_config_json(permission={**deny_permission(), "bash:*": "ask"})
        with mock.patch(
            "subprocess.run", side_effect=make_run_stub(second_config=second)
        ), mock.patch("subprocess.Popen") as popen:
            with self.assertRaises(ProviderNotConfiguredError):
                adapter.invoke(ollama_review_request())
        popen.assert_not_called()

    def test_extra_full_deny_tool_is_accepted(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        second = resolved_config_json(permission={**deny_permission(), "lsp": "deny"})
        with mock.patch(
            "subprocess.run", side_effect=make_run_stub(second_config=second)
        ), mock.patch(
            "subprocess.Popen", return_value=_completed_process(review_events())
        ):
            result = adapter.invoke(ollama_review_request())
        self.assertEqual(json.dumps({"approved": True, "findings": []}), result.output)

    def test_proxy_variables_are_removed_and_endpoint_bound(self) -> None:
        adapter = self._adapter(ollama_host="127.0.0.1:11434")
        captured: dict[str, object] = {}
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://proxy:8080",
                "HTTPS_PROXY": "http://proxy:8080",
                "ALL_PROXY": "http://proxy:8080",
            },
            clear=False,
        ):
            self.assertIn("HTTP_PROXY", os.environ)
            with mock.patch("subprocess.run", side_effect=make_run_stub()):

                def popen(command, **kwargs):
                    captured["command"] = command
                    captured["env"] = kwargs["env"]
                    captured["directory_writable"] = bool(
                        Path(command[command.index("--dir") + 1]).stat().st_mode & 0o222
                    )
                    return _completed_process(review_events())

                with mock.patch("subprocess.Popen", side_effect=popen):
                    adapter.invoke(ollama_review_request())
            # The parent environment is unchanged by the local subprocess cleanup.
            self.assertEqual("http://proxy:8080", os.environ["HTTP_PROXY"])
        child_env = captured["env"]
        self.assertNotIn("HTTP_PROXY", child_env)
        self.assertNotIn("HTTPS_PROXY", child_env)
        self.assertNotIn("ALL_PROXY", child_env)
        self.assertEqual("*", child_env["NO_PROXY"])
        self.assertEqual("*", child_env["no_proxy"])
        config = json.loads(child_env["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual("http://127.0.0.1:11434/v1", config["provider"]["ollama"]["options"]["baseURL"])
        self.assertFalse(captured["directory_writable"])


class LocalOllamaDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ollama_model()

    def _adapter(self, **kwargs) -> LocalOllamaReviewerAdapter:
        return LocalOllamaReviewerAdapter(
            planned_models=(self.model,),
            opencode_command="opencode-stub",
            ollama_host="127.0.0.1:11434",
            **kwargs,
        )

    def test_discovered_model_is_discoverable_with_zero_cost_not_callable(self) -> None:
        with mock.patch("subprocess.run", side_effect=make_run_stub()):
            records = self._adapter().discover()
        self.assertEqual(1, len(records))
        record = records[0]
        self.assertTrue(record.ref.is_local)
        self.assertEqual(PROVIDER, record.ref.provider)
        self.assertEqual("ollama-family", record.ref.family)
        self.assertIs(ModelAvailabilityState.DISCOVERABLE, record.availability_state)
        self.assertFalse(record.available)
        self.assertEqual(0, record.input_cost_per_million)
        self.assertEqual(0, record.output_cost_per_million)

    def test_unplanned_discovery_reports_unknown_family_not_gpt_oss(self) -> None:
        adapter = LocalOllamaReviewerAdapter(
            opencode_command="opencode-stub", ollama_host="127.0.0.1:11434"
        )
        with mock.patch(
            "subprocess.run", side_effect=make_run_stub(model_ids=("gpt-oss:120b", "other-model"))
        ):
            records = adapter.discover()
        families = {record.ref.family for record in records}
        self.assertEqual({None}, families)
        self.assertEqual(
            {"gpt-oss:120b", "other-model"}, {record.ref.model_id for record in records}
        )

    def test_model_list_unavailable_reports_planned_models_unavailable(self) -> None:
        def stub(args, **kwargs):
            if args[1] == "debug":
                return subprocess.CompletedProcess(args, 0, stdout=resolved_config_json(), stderr="")
            if args[1] == "models":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            raise AssertionError(args)

        with mock.patch("subprocess.run", side_effect=stub):
            records = self._adapter().discover()
        self.assertIs(ModelAvailabilityState.UNAVAILABLE, records[0].availability_state)

    def test_remote_resolved_endpoint_is_not_swallowed_by_discovery(self) -> None:
        adapter = self._adapter()
        with mock.patch("subprocess.run", side_effect=make_run_stub(base_url="http://evil.example:11434/v1")):
            with self.assertRaises(ProviderNotConfiguredError):
                adapter.discover()

    def test_require_model_raises_when_model_not_discovered(self) -> None:
        with mock.patch("subprocess.run", side_effect=make_run_stub(model_ids=("another-model",))):
            with self.assertRaisesRegex(ModelUnavailableError, "model unavailable"):
                self._adapter().require_model(self.model)

    def test_require_model_passes_for_discovered_loopback_model(self) -> None:
        with mock.patch("subprocess.run", side_effect=make_run_stub()):
            self._adapter().require_model(self.model)


class LocalOllamaGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ollama_model()

    def _adapter(self, **kwargs) -> LocalOllamaReviewerAdapter:
        return LocalOllamaReviewerAdapter(
            planned_models=(self.model,),
            opencode_command="opencode-stub",
            ollama_host="127.0.0.1:11434",
            **kwargs,
        )

    def test_write_roles_and_non_read_only_are_rejected_before_discovery(self) -> None:
        adapter = self._adapter()
        with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "local Ollama role denied"):
                adapter.invoke(ollama_review_request(role="implementation"))
            with self.assertRaisesRegex(ValueError, "local Ollama role denied"):
                adapter.invoke(ollama_review_request(role="revision"))
            with self.assertRaisesRegex(ValueError, "local Ollama role denied"):
                adapter.invoke(ollama_review_request(read_only=False))
        run.assert_not_called()
        popen.assert_not_called()

    def test_rereview_role_is_accepted(self) -> None:
        adapter = self._adapter()
        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", return_value=_completed_process(review_events())
        ):
            result = adapter.invoke(ollama_review_request(role="rereview"))
        self.assertEqual(json.dumps({"approved": True, "findings": []}), result.output)

    def test_constructor_rejects_remote_or_foreign_planned_model(self) -> None:
        with self.assertRaisesRegex(ValueError, "local Ollama"):
            LocalOllamaReviewerAdapter(planned_models=(ModelRef(PROVIDER, MODEL_ID, "1", None, False),))
        with self.assertRaisesRegex(ValueError, "local Ollama"):
            LocalOllamaReviewerAdapter(planned_models=(ModelRef("other", MODEL_ID, "1", None, True),))


class LocalOllamaInvokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ollama_model()

    def _adapter(self, **kwargs) -> LocalOllamaReviewerAdapter:
        return LocalOllamaReviewerAdapter(
            planned_models=(self.model,),
            opencode_command="opencode-stub",
            ollama_host="127.0.0.1:11434",
            **kwargs,
        )

    def _invoke(self, events: str, returncode: int = 0):
        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", return_value=_completed_process(events, returncode)
        ):
            return self._adapter().invoke(ollama_review_request())

    def test_packet_isolation_and_all_tools_denied(self) -> None:
        captured: dict[str, object] = {}

        def popen(command, **kwargs):
            captured["command"] = command
            captured["cwd"] = kwargs["cwd"]
            dir_arg = command[command.index("--dir") + 1]
            captured["directory_writable"] = bool(Path(dir_arg).stat().st_mode & 0o222)
            captured["config_content"] = kwargs["env"]["OPENCODE_CONFIG_CONTENT"]
            return _completed_process(review_events())

        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", side_effect=popen
        ):
            self._adapter().invoke(ollama_review_request())

        command = captured["command"]
        self.assertIsInstance(command, tuple)
        self.assertEqual(f"{PROVIDER}/{MODEL_ID}", command[command.index("--model") + 1])
        # The inference process starts in the same empty read-only temp dir it is
        # pointed at with --dir, never the repository's working directory.
        review_root = Path(command[command.index("--dir") + 1])
        self.assertEqual(str(review_root), captured["cwd"])
        self.assertNotEqual(Path.cwd().resolve(), review_root.resolve())
        self.assertFalse(captured["directory_writable"])
        config = json.loads(captured["config_content"])
        self.assertEqual(["ollama"], config["enabled_providers"])
        for name in DENY_TOOLS:
            self.assertEqual("deny", config["permission"][name])
            self.assertEqual(
                "deny",
                config["agent"]["agentflow-local-ollama-reviewer"]["permission"][name],
            )
        self.assertEqual(
            REMOTE_REVIEWER_MAX_STEPS,
            config["agent"]["agentflow-local-ollama-reviewer"]["steps"],
        )

    def test_local_call_reports_confirmed_zero_cost(self) -> None:
        result = self._invoke(review_events())
        self.assertEqual(0.0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_successful_review_records_token_usage(self) -> None:
        result = self._invoke(review_events(with_tokens=True))
        self.assertGreater(result.input_tokens, 0)
        self.assertGreater(result.output_tokens, 0)
        self.assertEqual("opencode_json_events", result.raw_metadata["token_source"])
        self.assertFalse(result.raw_metadata["usage_unavailable"])

    def test_positive_nonzero_exit_is_protocol_error_without_fallback(self) -> None:
        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen",
            return_value=_completed_process(review_events(with_tokens=True), returncode=1),
        ):
            with self.assertRaises(ReviewerProtocolError) as raised:
                self._adapter().invoke(ollama_review_request())
        result = raised.exception.result
        self.assertIsNotNone(result)
        self.assertEqual("nonzero_exit", result.raw_metadata["termination_reason"])
        self.assertEqual(0.0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_step_limit_stream_is_incomplete_not_unavailable(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", return_value=_completed_process(fixture.read_text(encoding="utf-8"))
        ):
            with self.assertRaises(InvocationIncompleteError) as raised:
                self._adapter().invoke(ollama_review_request())
        self.assertEqual("step_limit_reached", raised.exception.failure_kind)
        self.assertEqual(0.0, raised.exception.result.remote_cost)

    def test_timeout_is_unknown_with_local_zero_cost(self) -> None:
        class TimedOutProcess:
            pid = 50001
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired(("stub",), 1)
                return "", ""

        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", return_value=TimedOutProcess()
        ) as popen, mock.patch(
            "agentflow.opencode_adapter.OpenCodeAdapter.cancel", return_value=True
        ):
            with self.assertRaises(InvocationOutcomeUnknown) as raised:
                self._adapter(timeout_seconds=1).invoke(ollama_review_request())
        self.assertEqual(1, popen.call_count)
        self.assertEqual("timeout", raised.exception.termination_reason)
        self.assertIsNotNone(raised.exception.result)
        self.assertEqual(0.0, raised.exception.result.remote_cost)
        self.assertFalse(raised.exception.result.cost_unavailable)

    def test_interrupted_process_is_unknown(self) -> None:
        class InterruptedProcess:
            pid = 50002
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                return review_events(with_tokens=True), ""

        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", return_value=InterruptedProcess()
        ), mock.patch(
            "agentflow.opencode_adapter.OpenCodeAdapter.cancel", return_value=True
        ):
            with self.assertRaises(InvocationOutcomeUnknown) as raised:
                self._adapter().invoke(ollama_review_request())
        error = raised.exception
        self.assertEqual("interrupted", error.termination_reason)
        result = error.result
        self.assertIsNotNone(result)
        self.assertEqual("", result.output)
        self.assertEqual((7, 3), (result.input_tokens, result.output_tokens))
        self.assertEqual("ollama-session-1", result.provider_request_id)
        self.assertFalse(result.raw_metadata["usage_unavailable"])
        self.assertEqual(0.0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_signal_terminated_preserves_usage_evidence(self) -> None:
        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen",
            return_value=_completed_process(review_events(with_tokens=True), returncode=-15),
        ):
            with self.assertRaises(InvocationOutcomeUnknown) as raised:
                self._adapter().invoke(ollama_review_request())
        error = raised.exception
        self.assertEqual("signal_terminated", error.termination_reason)
        result = error.result
        self.assertIsNotNone(result)
        self.assertEqual("", result.output)
        self.assertEqual((7, 3), (result.input_tokens, result.output_tokens))
        self.assertEqual("ollama-session-1", result.provider_request_id)
        self.assertFalse(result.raw_metadata["usage_unavailable"])
        self.assertEqual(0.0, result.remote_cost)
        self.assertFalse(result.cost_unavailable)

    def test_communication_oserror_preserves_evidence(self) -> None:
        class OSErrorProcess:
            pid = 50003
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("broken pipe")
                return review_events(with_tokens=True), ""

        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", return_value=OSErrorProcess()
        ), mock.patch(
            "agentflow.opencode_adapter.OpenCodeAdapter.cancel", return_value=True
        ):
            with self.assertRaises(InvocationOutcomeUnknown) as raised:
                self._adapter().invoke(ollama_review_request())
        error = raised.exception
        self.assertEqual("communication_error", error.termination_reason)
        result = error.result
        self.assertIsNotNone(result)
        self.assertEqual("", result.output)
        self.assertEqual((7, 3), (result.input_tokens, result.output_tokens))
        self.assertEqual("ollama-session-1", result.provider_request_id)
        self.assertFalse(result.raw_metadata["usage_unavailable"])

    def test_interrupt_without_usage_is_marked_unavailable(self) -> None:
        class InterruptedNoUsageProcess:
            pid = 50004
            returncode = None

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise KeyboardInterrupt
                return "", ""

        with mock.patch("subprocess.run", side_effect=make_run_stub()), mock.patch(
            "subprocess.Popen", return_value=InterruptedNoUsageProcess()
        ), mock.patch(
            "agentflow.opencode_adapter.OpenCodeAdapter.cancel", return_value=True
        ):
            with self.assertRaises(InvocationOutcomeUnknown) as raised:
                self._adapter().invoke(ollama_review_request())
        result = raised.exception.result
        self.assertIsNotNone(result)
        self.assertEqual("", result.output)
        self.assertEqual((0, 0), (result.input_tokens, result.output_tokens))
        self.assertTrue(result.raw_metadata["usage_unavailable"])
        self.assertEqual("unavailable", result.raw_metadata["token_source"])


class UsageParseTests(unittest.TestCase):
    """Token/cost usage parsing semantics for the shared success path (O17/O18)."""

    def test_multi_step_tokens_accumulate(self) -> None:
        events = "\n".join(
            (
                review_text_event(),
                step_finish_event(5, 3),
                step_finish_event(7, 4),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual((12, 7), (result.input_tokens, result.output_tokens))

    def test_explicit_zero_tokens_is_confirmed_not_unknown(self) -> None:
        events = "\n".join(
            (
                review_text_event(),
                json.dumps({"type": "step_finish", "part": {"tokens": {"input": 0, "output": 0}}}),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual((0, 0), (result.input_tokens, result.output_tokens))
        self.assertFalse(result.raw_metadata["usage_unavailable"])
        self.assertEqual("opencode_json_events", result.raw_metadata["token_source"])

    def test_missing_tokens_is_marked_unavailable_not_zero(self) -> None:
        result = parse_opencode_json(review_text_event(), duration_ms=1, is_local=True)
        self.assertEqual((0, 0), (result.input_tokens, result.output_tokens))
        self.assertTrue(result.raw_metadata["usage_unavailable"])
        self.assertEqual("unavailable", result.raw_metadata["token_source"])

    def test_local_cost_is_confirmed_zero_and_remote_cost_validation_unchanged(self) -> None:
        local = parse_opencode_json(review_text_event(), duration_ms=1, is_local=True)
        self.assertEqual(0.0, local.remote_cost)
        self.assertFalse(local.cost_unavailable)
        remote = parse_opencode_json(review_text_event(), duration_ms=1, is_local=False)
        self.assertIsNone(remote.remote_cost)
        self.assertTrue(remote.cost_unavailable)

    def test_invalid_token_values_are_unknown_not_confirmed_zero(self) -> None:
        for tokens in (
            {"input": "bad", "output": None},
            {"input": True, "output": -2},
        ):
            with self.subTest(tokens=tokens):
                events = "\n".join(
                    (
                        review_text_event(),
                        json.dumps({"type": "step_finish", "part": {"tokens": tokens}}),
                    )
                )
                result = parse_opencode_json(events, duration_ms=1, is_local=True)
                self.assertEqual((0, 0), (result.input_tokens, result.output_tokens))
                self.assertTrue(result.raw_metadata["usage_unavailable"])
                self.assertEqual("unavailable", result.raw_metadata["token_source"])

    def test_reasoning_only_is_recorded_but_input_output_unknown(self) -> None:
        events = "\n".join(
            (
                review_text_event(),
                json.dumps({"type": "step_finish", "part": {"tokens": {"reasoning": 9}}}),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual(9, result.raw_metadata["reasoning_tokens"])
        self.assertEqual((0, 0), (result.input_tokens, result.output_tokens))
        self.assertTrue(result.raw_metadata["usage_unavailable"])

    def test_mixed_valid_and_invalid_events_preserve_confirmed_only(self) -> None:
        events = "\n".join(
            (
                review_text_event(),
                json.dumps({"type": "step_finish", "part": {"tokens": {"input": 5, "output": 3}}}),
                json.dumps(
                    {"type": "step_finish", "part": {"tokens": {"input": "bad", "output": 1}}}
                ),
            )
        )
        result = parse_opencode_json(events, duration_ms=1, is_local=True)
        self.assertEqual((5, 4), (result.input_tokens, result.output_tokens))
        self.assertTrue(result.raw_metadata["usage_unavailable"])
        self.assertEqual("unavailable", result.raw_metadata["token_source"])

    def _run_three_paths(self, events):
        text = "\n".join(json.dumps(payload) for payload in events)
        data = text.encode("utf-8")
        success = parse_opencode_json(text, duration_ms=1, is_local=True)
        failed = parse_opencode_failed_usage(
            data, duration_ms=1, is_local=False, termination_reason="nonzero_exit"
        )
        partial = parse_opencode_partial_usage(data, duration_ms=1, timeout_seconds=60)
        return success, failed, partial

    def _assert_usage(self, result, expected_tokens, expected_unavailable):
        self.assertEqual(expected_tokens, (result.input_tokens, result.output_tokens))
        self.assertEqual(
            expected_unavailable, result.raw_metadata["usage_unavailable"]
        )
        self.assertEqual(
            "unavailable" if expected_unavailable else "opencode_json_events",
            result.raw_metadata["token_source"],
        )

    def test_usage_availability_is_sticky_across_all_paths(self) -> None:
        text = {"type": "text", "part": {"type": "text", "text": "done"}}
        step_start = {"type": "step_start", "part": {}}

        def sf(tokens):
            return {"type": "step_finish", "part": {"tokens": tokens}}

        cases = (
            ("valid_then_invalid", [text, sf({"input": 5, "output": 3}), sf({"input": "bad", "output": 1})], (5, 4), True),
            ("invalid_then_valid", [text, sf({"input": "bad", "output": 1}), sf({"input": 5, "output": 3})], (5, 4), True),
            ("missing_output", [text, sf({"input": 5})], (5, 0), True),
            ("missing_input", [text, sf({"output": 3})], (0, 3), True),
            ("all_missing", [text, sf({})], (0, 0), True),
            ("reasoning_only", [text, sf({"reasoning": 9})], (0, 0), True),
            ("invalid_values", [text, sf({"input": "bad", "output": None})], (0, 0), True),
            ("explicit_zero", [text, sf({"input": 0, "output": 0})], (0, 0), False),
            ("multi_step_valid", [text, sf({"input": 5, "output": 3}), sf({"input": 7, "output": 4})], (12, 7), False),
            ("non_usage_events", [text, step_start, sf({"input": 5, "output": 3})], (5, 3), False),
        )
        for name, events, expected_tokens, expected_unavailable in cases:
            with self.subTest(case=name):
                success, failed, partial = self._run_three_paths(events)
                for result in (success, failed, partial):
                    self._assert_usage(result, expected_tokens, expected_unavailable)


class LocalOllamaRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = ollama_model()

    def test_router_keeps_default_route_and_denies_unknown_provider(self) -> None:
        fake = FakeAdapter()
        router = AdapterRouter(
            {"fake": fake}, {("ollama", "review"): FakeAdapter()}
        )
        self.assertIs(fake, router.adapter_for("fake"))
        self.assertIs(fake, router.adapter_for("fake", "implementation"))
        with self.assertRaises(ReviewerUnavailableError):
            router.adapter_for("ollama", "implementation")
        with self.assertRaises(ValueError):
            router.adapter_for("unknown")

    def test_cli_builds_review_only_role_adapter(self) -> None:
        task = replace(make_task("ollama-review"), review_model=self.model)
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_paths(directory)
            database = Database(paths.project_runs / "agentflow.db")
            with mock.patch("subprocess.run", side_effect=make_run_stub()):
                runner = _runner(paths, database, plan, authorization)
            router = runner.adapter
            self.assertIsInstance(router, AdapterRouter)
            self.assertIn(("ollama", "review"), router.role_adapters)
            self.assertIn(("ollama", "rereview"), router.role_adapters)
            self.assertNotIn("ollama", router.adapters)

    def test_cli_rejects_ollama_implementation_before_assembly(self) -> None:
        task = replace(
            make_task("ollama-review"), implementation_model=self.model
        )
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_paths(directory)
            database = Database(paths.project_runs / "agentflow.db")
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(UnsupportedProviderError):
                    _runner(paths, database, plan, authorization)
        run.assert_not_called()

    def test_cli_rejects_non_local_ollama_review_model(self) -> None:
        task = replace(
            make_task("ollama-review"),
            review_model=ModelRef(PROVIDER, MODEL_ID, "1", "ollama-family", False),
        )
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_paths(directory)
            database = Database(paths.project_runs / "agentflow.db")
            with mock.patch("subprocess.run") as run:
                with self.assertRaises(UnsupportedProviderError):
                    _runner(paths, database, plan, authorization)
        run.assert_not_called()

    def test_cli_rejects_provider_outside_authorization(self) -> None:
        task = replace(make_task("ollama-review"), review_model=self.model)
        plan = make_plan(tasks=(task,))
        authorization = replace(
            issue_authorization(plan), authorized_provider_ids=("fake",)
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = resolve_paths(directory)
            database = Database(paths.project_runs / "agentflow.db")
            with mock.patch("subprocess.run") as run:
                with self.assertRaisesRegex(ValueError, "outside the authorization"):
                    _runner(paths, database, plan, authorization)
        run.assert_not_called()


class LocalOllamaIntegrationTests(unittest.TestCase):
    """End-to-end Runner path with a fake implementer and a real local reviewer (O13-O16)."""

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

    def _router(self, adapter: LocalOllamaReviewerAdapter, **fake_kwargs) -> AdapterRouter:
        return AdapterRouter(
            {"fake": FakeAdapter(responder=approved_response, **fake_kwargs)},
            {("ollama", "review"): adapter, ("ollama", "rereview"): adapter},
        )

    def _subprocess_side_effects(self, *, review_stdout: str, returncode: int = 0):
        real_run = subprocess.run
        real_popen = subprocess.Popen

        def run_side_effect(args, **kwargs):
            if args and args[0] == "opencode-stub":
                if args[1] == "debug":
                    env = kwargs.get("env")
                    if env is not None and "OPENCODE_CONFIG_CONTENT" in env:
                        return subprocess.CompletedProcess(
                            args, 0, stdout=env["OPENCODE_CONFIG_CONTENT"], stderr=""
                        )
                    return subprocess.CompletedProcess(args, 0, stdout=resolved_config_json(), stderr="")
                if args[1] == "models":
                    return subprocess.CompletedProcess(args, 0, stdout=f"{PROVIDER}/{MODEL_ID}\n", stderr="")
                raise AssertionError(f"unexpected stub args: {args!r}")
            return real_run(args, **kwargs)

        def popen_side_effect(command, **kwargs):
            if command and command[0] == "opencode-stub":
                return _completed_process(review_stdout, returncode)
            return real_popen(command, **kwargs)

        return run_side_effect, popen_side_effect

    def test_review_and_rereview_complete_end_to_end(self) -> None:
        task = replace(make_task("ollama-review"), review_model=ollama_model())
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        adapter = LocalOllamaReviewerAdapter(
            planned_models=(task.review_model,),
            opencode_command="opencode-stub",
            ollama_host="127.0.0.1:11434",
            test_double=True,
        )
        run_side_effect, popen_side_effect = self._subprocess_side_effects(
            review_stdout=review_events()
        )
        with mock.patch("subprocess.run", side_effect=run_side_effect), mock.patch(
            "subprocess.Popen", side_effect=popen_side_effect
        ):
            result = Runner(self.database, self._router(adapter), self.workspace).start(
                plan, authorization, run_id="local-ollama-integration"
            )
        self.assertEqual(RunState.COMPLETED, result.state)
        review = self.database.fetch_one(
            "SELECT approved, provider FROM reviews WHERE run_id = 'local-ollama-integration'"
        )
        self.assertIsNotNone(review)
        self.assertEqual(1, review["approved"])
        self.assertEqual(PROVIDER, review["provider"])

    def test_ollama_fallback_for_write_role_is_never_invoked(self) -> None:
        def failing_impl(_request):
            raise ProviderNotConfiguredError("implementation provider not configured")

        task = replace(
            make_task("ollama-review"),
            review_model=ollama_model(),
            fallback_model=ollama_model(),
        )
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        adapter = LocalOllamaReviewerAdapter(
            planned_models=(task.review_model,),
            opencode_command="opencode-stub",
            ollama_host="127.0.0.1:11434",
            test_double=True,
        )
        router = AdapterRouter(
            {"fake": FakeAdapter(responder=failing_impl)},
            {("ollama", "review"): adapter, ("ollama", "rereview"): adapter},
        )
        real_run = subprocess.run
        real_popen = subprocess.Popen
        stub_calls: list[tuple] = []

        def run_side_effect(args, **kwargs):
            if args and args[0] == "opencode-stub":
                stub_calls.append(args)
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_run(args, **kwargs)

        def popen_side_effect(command, **kwargs):
            if command and command[0] == "opencode-stub":
                stub_calls.append(command)
                return _completed_process(review_events())
            return real_popen(command, **kwargs)

        with mock.patch("subprocess.run", side_effect=run_side_effect), mock.patch(
            "subprocess.Popen", side_effect=popen_side_effect
        ) as popen:
            result = Runner(self.database, router, self.workspace).start(
                plan, authorization, run_id="ollama-write-fallback"
            )
        self.assertEqual(RunState.PAUSED, result.state)
        # The local Ollama reviewer was never reached for a write role.
        self.assertEqual([], stub_calls)

    def test_step_limit_review_pauses_without_review_row_and_resume_is_blocked(self) -> None:
        fixture = Path(__file__).parent / "fixtures" / "opencode_max_steps.jsonl"
        task = replace(make_task("ollama-review"), review_model=ollama_model())
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        adapter = LocalOllamaReviewerAdapter(
            planned_models=(task.review_model,),
            opencode_command="opencode-stub",
            ollama_host="127.0.0.1:11434",
            test_double=True,
        )
        run_side_effect, popen_side_effect = self._subprocess_side_effects(
            review_stdout=fixture.read_text(encoding="utf-8")
        )
        runner = Runner(self.database, self._router(adapter), self.workspace)
        with mock.patch("subprocess.run", side_effect=run_side_effect), mock.patch(
            "subprocess.Popen", side_effect=popen_side_effect
        ):
            result = runner.start(plan, authorization, run_id="ollama-step-limit")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            0,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )
        with self.assertRaisesRegex(ValueError, "cannot be continued"):
            runner.resume("ollama-step-limit", plan, authorization)

    def test_signal_terminated_review_pauses_with_evidence_and_blocks_resume(self) -> None:
        task = replace(make_task("ollama-review"), review_model=ollama_model())
        plan = make_plan(tasks=(task,))
        authorization = issue_authorization(plan)
        adapter = LocalOllamaReviewerAdapter(
            planned_models=(task.review_model,),
            opencode_command="opencode-stub",
            ollama_host="127.0.0.1:11434",
            test_double=True,
        )
        run_side_effect, popen_side_effect = self._subprocess_side_effects(
            review_stdout=review_events(with_tokens=True), returncode=-15
        )
        runner = Runner(self.database, self._router(adapter), self.workspace)
        with mock.patch("subprocess.run", side_effect=run_side_effect), mock.patch(
            "subprocess.Popen", side_effect=popen_side_effect
        ):
            result = runner.start(plan, authorization, run_id="ollama-signal-review")
        self.assertEqual(RunState.PAUSED, result.state)
        self.assertEqual(
            0,
            self.database.fetch_one("SELECT COUNT(*) AS count FROM reviews")["count"],
        )
        call = self.database.fetch_one(
            "SELECT state, input_tokens, output_tokens, output_text, raw_metadata_json "
            "FROM model_calls WHERE role = 'review'"
        )
        self.assertEqual(InvocationState.UNKNOWN.value, call["state"])
        self.assertEqual((7, 3), (call["input_tokens"], call["output_tokens"]))
        self.assertEqual("", call["output_text"])
        metadata = json.loads(call["raw_metadata_json"])
        self.assertEqual("signal_terminated", metadata["termination_reason"])
        self.assertEqual("ollama-session-1", metadata["session_id"])
        self.assertFalse(metadata["usage_unavailable"])
        self.assertEqual(1, self.database.unresolved_unknown_calls("ollama-signal-review"))
        with self.assertRaisesRegex(ValueError, "must be reconciled"):
            runner.resume("ollama-signal-review", plan, authorization)


if __name__ == "__main__":
    unittest.main()
