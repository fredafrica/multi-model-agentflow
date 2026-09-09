"""OpenCode adapters for local execution and packet-only remote review."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from .resource_budgets import validate_invocation_budgets
from .serialization import canonical_json

from .adapters import (
    InvocationIncompleteError,
    InvocationProtocolError,
    InvocationOutcomeUnknown,
    ModelUnavailableError,
    ProviderNotConfiguredError,
    ReviewerProtocolError,
    ReviewerUnavailableError,
    UnsupportedProviderError,
    WorkerProtocolError,
)
from .contracts import (
    DEFAULT_IMPLEMENTATION_MAX_STEPS,
    DEFAULT_REVIEW_MAX_STEPS,
    REVIEW_MAX_STEPS_LIMIT,
    DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    DEFAULT_REMOTE_WORKER_MAX_STEPS,
    DEFAULT_REMOTE_WORKER_TIMEOUT_SECONDS,
    IMPLEMENTATION_MAX_STEPS_LIMIT,
    IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT,
    IMPLEMENTATION_TIMEOUT_SECONDS_MIN,
    REMOTE_WORKER_MAX_STEPS_LIMIT,
    REMOTE_WORKER_TIMEOUT_SECONDS_LIMIT,
    REMOTE_WORKER_TIMEOUT_SECONDS_MIN,
    BusinessImportance,
    InvocationRequest,
    InvocationResult,
    ModelAvailabilityState,
    ModelRecord,
    ModelRef,
    TrustLevel,
    validate_model_id,
    validate_provider_id,
)

# Legacy import compatibility only; invocation budgets come from the request.
REMOTE_REVIEWER_MAX_STEPS = DEFAULT_REVIEW_MAX_STEPS


class OpenCodeAdapter:
    adapter_id = "opencode-lmstudio"
    test_double = False

    def __init__(
        self,
        *,
        opencode_command: str | None = None,
        lms_command: str | None = None,
        timeout_seconds: int = 900,
    ) -> None:
        self.opencode_command = opencode_command or shutil.which("opencode") or "opencode"
        self.lms_command = lms_command or shutil.which("lms") or "lms"
        self.timeout_seconds = timeout_seconds

    def discover(self) -> Sequence[ModelRecord]:
        result = subprocess.run(
            (self.lms_command, "ps", "--json"),
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "LM Studio discovery failed")
        records: list[ModelRecord] = []
        for item in json.loads(result.stdout):
            if item.get("type") != "llm":
                continue
            model_id = str(item.get("identifier") or item["modelKey"])
            version = str(
                item.get("selectedVariant")
                or item.get("indexedModelIdentifier")
                or item.get("modelKey")
            )
            family = model_id.split("/", 1)[0]
            records.append(
                ModelRecord(
                    ref=ModelRef("lmstudio", model_id, version, family, True),
                    available=item.get("status") in {"idle", "generating"},
                    context_length=item.get("contextLength"),
                    tool_capable=bool(item.get("trainedForToolUse", False)),
                    input_cost_per_million=0,
                    output_cost_per_million=0,
                    measured_tokens_per_second=None,
                    trust_level=TrustLevel.UNVERIFIED,
                    highest_allowed_risk=BusinessImportance.NORMAL,
                    availability_state=(
                        ModelAvailabilityState.CALLABLE_UNVERIFIED
                        if item.get("status") in {"idle", "generating"}
                        else ModelAvailabilityState.UNAVAILABLE
                    ),
                )
            )
        return records

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        if request.model.provider != "lmstudio":
            raise ValueError("OpenCodeAdapter only permits the local lmstudio provider")
        if not request.model.is_local:
            raise ValueError("lmstudio models must be marked local")
        if request.role in {"review", "rereview"} and not request.read_only:
            raise ValueError("LM Studio review requires read_only=true")
        worktree = Path(str(request.metadata["worktree"])).resolve()
        if not worktree.is_dir():
            raise ValueError("invocation worktree does not exist")
        steps = (_review_steps(request.metadata) if request.role in {"review", "rereview"}
                 else _implementation_steps(request.metadata))
        timeout_seconds = _implementation_timeout(request.metadata)
        output_budget = validate_invocation_budgets(request)
        prompt = self._bounded_prompt(request)
        environment = os.environ.copy()
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            self._permission_config(read_only=request.read_only, steps=steps),
            separators=(",", ":"),
        )
        _prepare_output_environment(self.opencode_command, worktree, environment, request, output_budget)
        command = [
            self.opencode_command,
            "run",
            "--format",
            "json",
            "--pure",
            "--auto",
            "--agent",
            "agentflow-sandbox",
            "--model",
            f"lmstudio/{request.model.model_id}",
        ]
        session_id = _continuation_session(request.metadata)
        if session_id is not None:
            command.extend(("--session", session_id))
        command.extend(("--dir", str(worktree), prompt))
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        process_reference = f"local-process-group:{process.pid}"
        callback = request.metadata.get("on_provider_request_id")
        if callable(callback):
            callback(process_reference)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self.cancel(process_reference)
            partial_stdout, cleanup_incomplete = _collect_timeout_output(error, process)
            duration_ms = int((time.monotonic() - started) * 1000)
            result = parse_opencode_partial_usage(
                partial_stdout,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
                cleanup_incomplete=cleanup_incomplete,
            )
            raise InvocationOutcomeUnknown(
                "OpenCode timed out; inspect the local worktree before retrying",
                process_reference,
                result=result,
                termination_reason="timeout",
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if process.returncode is not None and process.returncode < 0:
            raise InvocationOutcomeUnknown(
                "OpenCode was terminated by a signal; inspect the local worktree "
                "before retrying",
                process_reference,
                termination_reason="signal_terminated",
            )
        if process.returncode:
            try:
                parse_opencode_json(
                    stdout,
                    duration_ms=duration_ms,
                    is_local=True,
                    configured_step_limit=steps,
                )
            except InvocationIncompleteError as error:
                if session_id is not None:
                    _verify_session_reuse(session_id, error.result)
                raise
            except InvocationProtocolError:
                raise
            except RuntimeError:
                pass
            failed_usage = parse_opencode_failed_usage(
                _as_output_bytes(stdout), duration_ms=duration_ms, is_local=True,
                termination_reason="nonzero_exit",
            )
            raise InvocationProtocolError(
                stderr.strip() or "OpenCode invocation failed", result=failed_usage
            )
        result = parse_opencode_json(
            stdout,
            duration_ms=duration_ms,
            is_local=True,
            configured_step_limit=steps,
        )
        if session_id is not None:
            _verify_session_reuse(session_id, result)
        return result

    def query(self, provider_request_id: str) -> InvocationResult | None:
        return None

    def cancel(self, provider_request_id: str) -> bool:
        prefixes = ("local-process-group:", "opencode-process-group:")
        prefix = next(
            (item for item in prefixes if provider_request_id.startswith(item)), None
        )
        if prefix is None:
            return False
        try:
            process_group = int(provider_request_id.removeprefix(prefix))
            os.killpg(process_group, signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            return False
        return True

    @staticmethod
    def _permission_config(*, read_only: bool, steps: int = DEFAULT_IMPLEMENTATION_MAX_STEPS) -> dict[str, Any]:
        permission = {
            "*": "deny",
            "read": {
                "*": "allow",
                ".env": "deny",
                ".env.*": "deny",
                "**/.env": "deny",
                "**/.env.*": "deny",
            },
            "glob": "allow",
            "grep": "allow",
            "edit": "deny" if read_only else "allow",
            "write": "deny" if read_only else "allow",
            "bash": "deny",
            "shell": "deny",
            "external_directory": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "task": "deny",
            "subagent": "deny",
            "skill": "deny",
            "question": "deny",
        }
        if read_only:
            permission = _reviewer_deny_permission()
        return {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": ["lmstudio"],
            "permission": permission,
            "agent": {
                "agentflow-sandbox": {
                    "description": "Bounded local AgentFlow task",
                    "mode": "primary",
                    "steps": steps,
                    "permission": permission,
                }
            },
        }

    @staticmethod
    def _bounded_prompt(request: InvocationRequest) -> str:
        allowed = "\n".join(f"- {item}" for item in request.metadata.get("allowed_files", ()))
        mode = (
            "Packet-only read-only review. All tools are disabled. "
            "Review only the supplied material; report missing evidence without reading files."
            if request.read_only
            else f"You may edit only these project-relative files:\n{allowed}"
        )
        return (
            "You are running inside an isolated Git worktree. "
            "Do not access external directories or networks.\n"
            f"{mode}\n\n{request.prompt}"
        )


def _discover_remote_model_ids(
    provider: str,
    opencode_command: str,
    *,
    discovery_timeout_seconds: int,
) -> tuple[str, ...]:
    """Return the configured model IDs for a remote provider, without inference."""
    try:
        result = subprocess.run(
            (opencode_command, "models", provider, "--pure"),
            text=True,
            capture_output=True,
            check=False,
            timeout=discovery_timeout_seconds,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise UnsupportedProviderError(
            "unsupported provider: OpenCode model discovery is unavailable"
        ) from error
    except OSError as error:
        raise UnsupportedProviderError(
            "unsupported provider: OpenCode model discovery is unavailable"
        ) from error
    if result.returncode:
        raise ProviderNotConfiguredError(
            f"provider not configured: {provider}"
        )
    prefix = f"{provider}/"
    model_ids = tuple(
        line.strip().removeprefix(prefix)
        for line in result.stdout.splitlines()
        if line.strip().startswith(prefix)
    )
    if not model_ids:
        raise ProviderNotConfiguredError(
            f"provider not configured: {provider}"
        )
    for model_id in model_ids:
        validate_model_id(model_id)
    return model_ids


_DEFAULT_OLLAMA_PORT = 11434
_DEFAULT_OLLAMA_PATH = "/v1"
_DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/v1"


def _normalize_loopback_host(host: str) -> str:
    """Return the numeric loopback host for ``host``, or raise ``ValueError``.

    Only the case-insensitive exact ``localhost`` name, an IPv4 address in
    ``127.0.0.0/8``, or the IPv6 loopback ``::1`` (including equivalent expanded
    forms) is accepted. Everything else -- including ``localhost.``, subdomains,
    IPv4-mapped IPv6, IPv6 zone identifiers, and non-loopback addresses -- is
    rejected. ``localhost`` is normalized to ``127.0.0.1`` so no DNS lookup is
    ever needed to compare endpoints.
    """
    if not host:
        raise ValueError("Ollama endpoint host is missing")
    if "%" in host:
        raise ValueError("Ollama endpoint must not include an IPv6 zone identifier")
    if host.lower() == "localhost":
        return "127.0.0.1"
    try:
        ipv4 = ipaddress.IPv4Address(host)
    except ValueError:
        ipv4 = None
    if ipv4 is not None:
        if not ipv4.is_loopback:
            raise ValueError("Ollama endpoint host is not a loopback address")
        return str(ipv4)
    try:
        ipv6 = ipaddress.IPv6Address(host)
    except ValueError:
        ipv6 = None
    if ipv6 is not None:
        if ipv6.ipv4_mapped is not None:
            raise ValueError("Ollama endpoint must not use an IPv4-mapped IPv6 address")
        if not ipv6.is_loopback:
            raise ValueError("Ollama endpoint host is not a loopback address")
        return str(ipv6)
    raise ValueError("Ollama endpoint host is not a loopback address")


def _parse_decimal_port(text: str) -> int:
    """Parse a strict decimal port string, rejecting empty/non-decimal values."""
    if not text or not text.isdigit():
        raise ValueError("Ollama endpoint port must be a decimal integer")
    return int(text)


def _parse_bare_ollama_endpoint(value: str) -> tuple[str, int | None, str]:
    """Split a scheme-less endpoint into ``(host, port, path)``.

    ``value`` has already been checked for internal whitespace/control
    characters and backslashes. A bare IPv6 address (which may contain many
    colons) is recognized with :func:`ipaddress.IPv6Address` before any
    ``host:port`` splitting, so ``::1`` is never misread as a host with a port.
    """
    if value.startswith("["):
        close = value.find("]")
        if close == -1:
            raise ValueError("Ollama endpoint IPv6 brackets are unbalanced")
        host = value[1:close]
        rest = value[close + 1:]
        if not rest:
            return host, None, ""
        if not rest.startswith(":"):
            raise ValueError("Ollama endpoint has an invalid bracketed-host suffix")
        return host, _parse_decimal_port(rest[1:]), ""
    if ":" in value:
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            pass
        else:
            return value, None, ""
        host, _, port_text = value.rpartition(":")
        return host, _parse_decimal_port(port_text), ""
    return value, None, ""


def _normalize_ollama_endpoint(raw: object) -> str:
    """Normalize a local Ollama endpoint to a canonical URL, or raise ``ValueError``.

    Accepts a full ``http``/``https`` URL, a bare IPv4/localhost name, a
    ``host:port`` pair, a bare IPv6 address, or a bracketed IPv6 address with an
    optional port. ``None``, an empty string, or pure whitespace yields the
    deterministic Ollama default ``http://127.0.0.1:11434/v1``.

    Only loopback hosts are accepted. Any other domain, ``localhost.``,
    subdomain, IPv4-mapped IPv6, IPv6 zone id, unbalanced bracket, non-http(s)
    scheme, userinfo, query, fragment, backslash, internal whitespace/control
    character, or non-decimal/out-of-range port raises ``ValueError``. A missing
    scheme defaults to ``http``, a missing port to ``11434``, and a missing or
    root path to ``/v1``; an explicit non-root path is preserved verbatim. The
    returned value is a canonical ``scheme://host:port/path`` string with the
    host normalized to a numeric loopback form, never the raw input (which may
    contain credentials and must not leak into an error message).
    """
    if raw is None:
        return _DEFAULT_OLLAMA_ENDPOINT
    if not isinstance(raw, str):
        raise ValueError("Ollama endpoint must be a string or None")
    value = raw.strip()
    if not value:
        return _DEFAULT_OLLAMA_ENDPOINT
    if any(ch.isspace() or ord(ch) < 0x20 for ch in value):
        raise ValueError("Ollama endpoint contains whitespace or control characters")
    if "\\" in value:
        raise ValueError("Ollama endpoint must not contain backslashes")
    scheme = "http"
    host: str
    port: int | None
    path: str
    if "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError as error:
            raise ValueError("Ollama endpoint URL is invalid") from error
        if parsed.scheme.lower() not in ("http", "https"):
            raise ValueError("Ollama endpoint scheme must be http or https")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Ollama endpoint must not include userinfo")
        # An empty query or fragment marker ("?" or "#") is still a query/fragment.
        if "?" in value or "#" in value:
            raise ValueError("Ollama endpoint must not include a query or fragment")
        host = parsed.hostname or ""
        # A written-but-empty port ("http://localhost:/v1") has an authority ending
        # in ":"; it is distinct from a completely absent port, which is filled with
        # the default below and must not be silently treated as if it were missing.
        if parsed.netloc.endswith(":"):
            raise ValueError("Ollama endpoint must not include an empty port")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("Ollama endpoint port is invalid") from error
        path = parsed.path
        scheme = parsed.scheme.lower()
    else:
        host, port, path = _parse_bare_ollama_endpoint(value)
    normalized_host = _normalize_loopback_host(host)
    if port is None:
        port = _DEFAULT_OLLAMA_PORT
    if not 1 <= port <= 65535:
        raise ValueError("Ollama endpoint port must be between 1 and 65535")
    if path in ("", "/"):
        path = _DEFAULT_OLLAMA_PATH
    authority = f"[{normalized_host}]" if ":" in normalized_host else normalized_host
    return f"{scheme}://{authority}:{port}{path}"


def ollama_host_is_loopback(raw: object) -> bool:
    """Return whether an Ollama endpoint is a deterministic local loopback.

    ``raw`` is the value of ``OLLAMA_HOST`` (a URL, a ``host:port`` pair, a bare
    host, or ``None``/empty meaning the deterministic Ollama default of
    ``127.0.0.1:11434``). Only loopback endpoints (``localhost``, the IPv4
    ``127.0.0.0/8`` range, and the IPv6 ``::1``) count as local; anything else --
    including non-loopback hosts, malformed URLs, bad ports, and non-string
    inputs -- returns ``False`` so the caller can fail closed rather than record
    a remote endpoint as a free local call. Parsing is purely lexical via
    :func:`ipaddress` and :func:`urllib.parse.urlsplit`: no DNS resolution or
    network probe is performed.
    """
    try:
        _normalize_ollama_endpoint(raw)
    except ValueError:
        return False
    return True


def _run_opencode_packet_review(
    *,
    command_prefix: tuple[str, ...],
    config_content: str,
    prompt: str,
    timeout_seconds: int,
    is_local: bool,
    review_label: str,
    model_label: str = "",
    configured_steps: int = DEFAULT_REVIEW_MAX_STEPS,
    on_provider_request_id=None,
    prepare_environment: Callable[[Path, dict[str, str]], None] | None = None,
) -> InvocationResult:
    """Run a packet-only OpenCode review process and parse its outcome.

    This is the shared execution path for every packet-only reviewer (remote
    providers and the local Ollama provider): a fresh, read-only temp directory is
    used as ``--dir`` so the model never sees a repository, the bounded permission
    config is injected via ``OPENCODE_CONFIG_CONTENT``, and timeout / interrupt /
    signal / non-zero-exit / step-limit semantics are handled identically. ``is_local``
    drives both the usage parse and the timeout cost: a local call always reports a
    confirmed zero remote cost, while a remote call marks the cost unavailable when
    the provider did not report one.

    A ``prepare_environment`` callback, when supplied, runs after the temp dir is
    created but before ``Popen``. It receives the review root and the environment
    that will be handed to the child process, and may overwrite
    ``OPENCODE_CONFIG_CONTENT`` (for example to bind a verified local endpoint) or
    fail closed by raising a ``ReviewerUnavailableError``. Remote reviewers pass no
    callback and keep the plain config content.
    """
    environment = os.environ.copy()
    environment["OPENCODE_CONFIG_CONTENT"] = config_content
    with tempfile.TemporaryDirectory(prefix="agentflow-review-") as directory:
        review_root = Path(directory)
        review_root.chmod(0o500)
        if prepare_environment is not None:
            prepare_environment(review_root, environment)
        command: tuple[str, ...] = (*command_prefix, "--dir", str(review_root), prompt)
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            cwd=str(review_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        process_reference = f"opencode-process-group:{process.pid}"
        if callable(on_provider_request_id):
            on_provider_request_id(process_reference)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            OpenCodeAdapter().cancel(process_reference)
            partial_stdout, cleanup_incomplete = _collect_timeout_output(error, process)
            duration_ms = int((time.monotonic() - started) * 1000)
            result = parse_opencode_partial_usage(
                partial_stdout,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
                cost_unavailable=not is_local,
                cleanup_incomplete=cleanup_incomplete,
            )
            raise InvocationOutcomeUnknown(
                f"{review_label} timed out; no result is available",
                process_reference,
                result=result,
                termination_reason="timeout",
            ) from error
        except KeyboardInterrupt as error:
            OpenCodeAdapter().cancel(process_reference)
            drained, cleanup_incomplete = _bounded_drain(process)
            duration_ms = int((time.monotonic() - started) * 1000)
            result = parse_opencode_partial_usage(
                drained,
                duration_ms=duration_ms,
                cost_unavailable=not is_local,
                cleanup_incomplete=cleanup_incomplete,
                termination_reason="interrupted",
            )
            raise InvocationOutcomeUnknown(
                f"invocation outcome unknown: {review_label} was interrupted",
                process_reference,
                result=result,
                termination_reason="interrupted",
            ) from error
        except OSError as error:
            OpenCodeAdapter().cancel(process_reference)
            drained, cleanup_incomplete = _bounded_drain(process)
            duration_ms = int((time.monotonic() - started) * 1000)
            result = parse_opencode_partial_usage(
                drained,
                duration_ms=duration_ms,
                cost_unavailable=not is_local,
                cleanup_incomplete=cleanup_incomplete,
                termination_reason="communication_error",
            )
            raise InvocationOutcomeUnknown(
                f"invocation outcome unknown: {review_label} communication failed",
                process_reference,
                result=result,
                termination_reason="communication_error",
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if process.returncode is not None and process.returncode < 0:
            result = parse_opencode_partial_usage(
                _as_output_bytes(stdout),
                duration_ms=duration_ms,
                cost_unavailable=not is_local,
                termination_reason="signal_terminated",
            )
            raise InvocationOutcomeUnknown(
                f"invocation outcome unknown: {review_label} was terminated",
                process_reference,
                result=result,
                termination_reason="signal_terminated",
            )
        if process.returncode:
            try:
                parse_opencode_json(
                    stdout, duration_ms=duration_ms, is_local=is_local,
                    configured_step_limit=configured_steps,
                )
            except InvocationIncompleteError:
                raise
            except RuntimeError:
                pass
            failed_usage = parse_opencode_failed_usage(
                _as_output_bytes(stdout),
                duration_ms=duration_ms,
                is_local=is_local,
                termination_reason="nonzero_exit",
            )
            raise ReviewerProtocolError(
                f"{review_label} failed: " + (stderr.strip() or model_label),
                result=failed_usage,
            )
        try:
            return parse_opencode_json(
                stdout, duration_ms=duration_ms, is_local=is_local,
                configured_step_limit=configured_steps,
            )
        except InvocationIncompleteError:
            raise
        except RuntimeError as error:
            failed_usage = parse_opencode_failed_usage(
                _as_output_bytes(stdout),
                duration_ms=duration_ms,
                is_local=is_local,
                termination_reason="no_usable_result",
            )
            raise ReviewerProtocolError(
                f"{review_label} returned no usable result", result=failed_usage
            ) from error


class RemoteOpenCodeReviewerAdapter:
    """Run an authorized remote reviewer with no repository or tool access."""

    def __init__(
        self,
        provider: str,
        *,
        planned_models: Sequence[ModelRef] = (),
        opencode_command: str | None = None,
        timeout_seconds: int = 900,
        discovery_timeout_seconds: int = 15,
        test_double: bool = False,
    ) -> None:
        self.provider = validate_provider_id(provider)
        if self.provider in {"fake", "lmstudio"}:
            raise UnsupportedProviderError(
                f"unsupported provider for remote review: {self.provider}"
            )
        for model in planned_models:
            if model.provider != self.provider or model.is_local:
                raise ValueError("planned remote models must match the adapter provider")
        self.planned_models = tuple(planned_models)
        self.opencode_command = opencode_command or shutil.which("opencode") or "opencode"
        self.timeout_seconds = timeout_seconds
        self.discovery_timeout_seconds = discovery_timeout_seconds
        self.test_double = test_double
        self.adapter_id = f"opencode-remote-review:{self.provider}"

    def _discover_model_ids(self) -> tuple[str, ...]:
        return _discover_remote_model_ids(
            self.provider,
            self.opencode_command,
            discovery_timeout_seconds=self.discovery_timeout_seconds,
        )

    def discover(self) -> Sequence[ModelRecord]:
        discovered = set(self._discover_model_ids())
        refs = self.planned_models or tuple(
            ModelRef(self.provider, model_id, model_id, is_local=False)
            for model_id in sorted(discovered)
        )
        return tuple(
            ModelRecord(
                ref=model,
                available=False,
                context_length=None,
                tool_capable=False,
                input_cost_per_million=None,
                output_cost_per_million=None,
                measured_tokens_per_second=None,
                trust_level=TrustLevel.UNVERIFIED,
                highest_allowed_risk=BusinessImportance.NORMAL,
                availability_state=(
                    ModelAvailabilityState.DISCOVERABLE
                    if model.model_id in discovered
                    else ModelAvailabilityState.UNAVAILABLE
                ),
            )
            for model in refs
        )

    def require_model(self, model: ModelRef) -> None:
        if model.provider != self.provider or model.is_local:
            raise UnsupportedProviderError(
                f"unsupported provider/model locality: {model.provider}"
            )
        if model.model_id not in self._discover_model_ids():
            raise ModelUnavailableError(
                f"model unavailable: {self.provider}/{model.model_id}"
            )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        if request.role not in {"review", "rereview"}:
            raise ValueError("remote role denied: only review and rereview are allowed")
        if not request.read_only:
            raise ValueError("remote role denied: reviewer must be read-only")
        steps = _review_steps(request.metadata)
        output_budget = validate_invocation_budgets(request)
        self.require_model(request.model)
        model_label = f"{self.provider}/{request.model.model_id}"
        command_prefix = (
            self.opencode_command,
            "run",
            "--format",
            "json",
            "--pure",
            "--auto",
            "--agent",
            "agentflow-remote-reviewer",
            "--model",
            model_label,
        )
        return _run_opencode_packet_review(
            command_prefix=command_prefix,
            config_content=json.dumps(
                self._permission_config(steps=steps), separators=(",", ":")
            ),
            prompt=request.prompt,
            timeout_seconds=self.timeout_seconds,
            is_local=False,
            review_label="remote OpenCode review",
            configured_steps=steps,
            model_label=model_label,
            on_provider_request_id=request.metadata.get("on_provider_request_id"),
            prepare_environment=lambda root, environment: _prepare_output_environment(
                self.opencode_command, root, environment, request, output_budget
            ),
        )

    def query(self, provider_request_id: str) -> InvocationResult | None:
        return None

    def cancel(self, provider_request_id: str) -> bool:
        return OpenCodeAdapter().cancel(provider_request_id)

    def _permission_config(self, *, steps: int = DEFAULT_REVIEW_MAX_STEPS) -> dict[str, Any]:
        permission = {
            "*": "deny",
            "read": "deny",
            "glob": "deny",
            "grep": "deny",
            "edit": "deny",
            "write": "deny",
            "bash": "deny",
            "shell": "deny",
            "external_directory": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "task": "deny",
            "subagent": "deny",
            "skill": "deny",
            "question": "deny",
        }
        return {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": [self.provider],
            "permission": permission,
            "agent": {
                "agentflow-remote-reviewer": {
                    "description": "Packet-only read-only AgentFlow reviewer",
                    "mode": "primary",
                    "steps": steps,
                    "permission": permission,
                }
            },
        }


_OLLAMA_DENY_TOOLS = (
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


def _reviewer_deny_permission() -> dict[str, Any]:
    """A deny-by-default permission object with every tool explicitly denied."""
    return dict.fromkeys(_OLLAMA_DENY_TOOLS, "deny", ) | {"*": "deny"}


def _local_ollama_permission_config(
    *, model_id: str, endpoint: str, model_entry: Mapping[str, Any] | None,
    steps: int = DEFAULT_REVIEW_MAX_STEPS,
) -> dict[str, Any]:
    """Build the child config that binds a verified loopback Ollama endpoint.

    The selected model's existing metadata is preserved, but its ``options.baseURL``
    is pinned to the verified endpoint so no higher-precedence config can override
    the endpoint back to a remote address. Both the global and the agent permission
    layers deny every tool, and ``enabled_providers`` is exactly ``ollama``.
    """
    permission = _reviewer_deny_permission()
    model_config = dict(model_entry) if model_entry else {}
    model_options = model_config.get("options")
    if not isinstance(model_options, Mapping):
        model_options = {}
    model_config["options"] = {**dict(model_options), "baseURL": endpoint}
    return {
        "$schema": "https://opencode.ai/config.json",
        "enabled_providers": ["ollama"],
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": endpoint},
                "models": {model_id: model_config},
            }
        },
        "permission": permission,
        "agent": {
            "agentflow-local-ollama-reviewer": {
                "description": "Packet-only read-only local Ollama reviewer",
                "mode": "primary",
                "steps": steps,
                "permission": permission,
            }
        },
    }


def _read_resolved_config(
    opencode_command: str,
    review_root: Path,
    timeout_seconds: int,
    *,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Return the stdout of ``opencode debug config --pure``, failing closed.

    A missing executable or an un-runnable config-discovery command is an
    ``UnsupportedProviderError``; a config-discovery timeout is a
    ``ProviderNotConfiguredError``. The full resolved config is never logged or
    embedded in an error message.
    """
    try:
        result = subprocess.run(
            (opencode_command, "debug", "config", "--pure"),
            cwd=str(review_root),
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
            env=None if environment is None else dict(environment),
        )
    except FileNotFoundError as error:
        raise UnsupportedProviderError("OpenCode executable is unavailable") from error
    except PermissionError as error:
        raise UnsupportedProviderError("OpenCode executable is not runnable") from error
    except subprocess.TimeoutExpired as error:
        raise ProviderNotConfiguredError("OpenCode config discovery timed out") from error
    except OSError as error:
        raise UnsupportedProviderError(
            "OpenCode config discovery could not run"
        ) from error
    if result.returncode:
        raise UnsupportedProviderError("OpenCode config discovery failed to run")
    return result.stdout


def _parse_resolved_ollama_config(
    text: str, *, model_id: str | None = None
) -> dict[str, Any]:
    """Parse a resolved OpenCode config and return the Ollama summary.

    Reads ``provider.ollama`` (``npm``, ``options.baseURL``) and, when a model ID
    is requested, the selected model entry. The ``npm`` must be
    ``@ai-sdk/openai-compatible`` and the base URL must normalize to a local
    loopback. Any missing section, invalid JSON, unknown transport, or
    non-loopback endpoint is a controlled ``ProviderNotConfiguredError``.
    """
    try:
        config = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderNotConfiguredError(
            "OpenCode resolved config is not valid JSON"
        ) from error
    if not isinstance(config, Mapping):
        raise ProviderNotConfiguredError("OpenCode resolved config must be a JSON object")
    providers = config.get("provider")
    if not isinstance(providers, Mapping):
        raise ProviderNotConfiguredError("resolved config has no provider section")
    ollama = providers.get("ollama")
    if not isinstance(ollama, Mapping):
        raise ProviderNotConfiguredError("Ollama provider is not configured")
    npm = ollama.get("npm")
    if npm != "@ai-sdk/openai-compatible":
        raise ProviderNotConfiguredError("unsupported Ollama transport")
    options = ollama.get("options")
    if not isinstance(options, Mapping):
        raise ProviderNotConfiguredError("Ollama provider options are missing")
    base_url = options.get("baseURL")
    if not isinstance(base_url, str) or not base_url.strip():
        raise ProviderNotConfiguredError("Ollama provider baseURL is missing")
    try:
        endpoint = _normalize_ollama_endpoint(base_url)
    except ValueError as error:
        raise ProviderNotConfiguredError(
            "Ollama provider baseURL is not a local loopback"
        ) from error
    model_entry = None
    models = ollama.get("models")
    if model_id is not None and isinstance(models, Mapping):
        candidate = models.get(model_id)
        if isinstance(candidate, Mapping):
            model_entry = candidate
    enabled = config.get("enabled_providers")
    enabled_providers = tuple(enabled) if isinstance(enabled, (list, tuple)) else None
    return {
        "npm": npm,
        "endpoint": endpoint,
        "model_entry": model_entry,
        "enabled_providers": enabled_providers,
        "permission": config.get("permission"),
        "agent": config.get("agent"),
    }


def _check_endpoint_override(raw: object, endpoint: str, label: str) -> None:
    if not isinstance(raw, str) or not raw.strip():
        raise ProviderNotConfiguredError(f"{label} is not a valid string")
    try:
        normalized = _normalize_ollama_endpoint(raw)
    except ValueError as error:
        raise ProviderNotConfiguredError(f"{label} is not a local loopback") from error
    if normalized != endpoint:
        raise ProviderNotConfiguredError(f"{label} conflicts with the provider endpoint")


def _validate_model_entry_endpoint(
    model_entry: Mapping[str, Any] | None, endpoint: str
) -> None:
    if model_entry is None:
        return
    options = model_entry.get("options")
    if isinstance(options, Mapping) and options.get("baseURL") is not None:
        _check_endpoint_override(options["baseURL"], endpoint, "model options.baseURL")
    api = model_entry.get("api")
    if isinstance(api, Mapping) and api.get("url") is not None:
        _check_endpoint_override(api["url"], endpoint, "model api.url")


def _validate_explicit_endpoint(raw: object, endpoint: str) -> None:
    if raw is None:
        return
    if not isinstance(raw, str) or not raw.strip():
        return
    try:
        normalized = _normalize_ollama_endpoint(raw)
    except ValueError as error:
        raise ProviderNotConfiguredError("OLLAMA_HOST is not a local loopback") from error
    if normalized != endpoint:
        raise ProviderNotConfiguredError(
            "OLLAMA_HOST conflicts with the resolved provider endpoint"
        )


def _verify_deny_permission(permission: Any, label: str) -> None:
    if not isinstance(permission, Mapping):
        raise ProviderNotConfiguredError(f"resolved {label} is missing")
    if permission.get("*") != "deny":
        raise ProviderNotConfiguredError(f"resolved {label} is not deny-by-default")
    # Every key and value must be a strict deny. An unknown tool, a pattern, a
    # nested rule object, or an ``allow``/``ask`` value is ambiguous or a widening
    # grant and must fail closed; the reviewer has no legitimate positive grant.
    for tool, value in permission.items():
        if value != "deny":
            raise ProviderNotConfiguredError(
                f"resolved {label} grants or leaves ambiguous permission for {tool}"
            )


def _verify_local_config_consistency(
    parsed: Mapping[str, Any], endpoint: str, *, steps: int = DEFAULT_REVIEW_MAX_STEPS,
    output_budget: Mapping[str, Any] | None = None,
) -> None:
    """Re-verify a resolved config after injecting the verified child config.

    Confirms a higher-precedence (e.g. managed) override did not change the
    transport, change or override the endpoint back to remote, widen
    ``enabled_providers``, alter the reviewer steps, or re-introduce an ``allow``
    permission on either the global or the agent permission layer.
    """
    if parsed["npm"] != "@ai-sdk/openai-compatible":
        raise ProviderNotConfiguredError("resolved transport changed during configuration")
    if parsed["endpoint"] != endpoint:
        raise ProviderNotConfiguredError("resolved endpoint changed during configuration")
    if parsed["enabled_providers"] != ("ollama",):
        raise ProviderNotConfiguredError("resolved enabled_providers is not exactly ollama")
    _verify_deny_permission(parsed["permission"], "global permission")
    agent = parsed["agent"]
    if not isinstance(agent, Mapping):
        raise ProviderNotConfiguredError("resolved config lost the reviewer agent")
    local_agent = agent.get("agentflow-local-ollama-reviewer")
    if not isinstance(local_agent, Mapping):
        raise ProviderNotConfiguredError("resolved config lost the local reviewer agent")
    if type(local_agent.get("steps")) is not int or local_agent["steps"] != steps:
        raise ProviderNotConfiguredError("resolved reviewer steps changed")
    _verify_deny_permission(local_agent.get("permission"), "agent permission")
    if output_budget is not None:
        entry = parsed.get("model_entry")
        limit = entry.get("limit") if isinstance(entry, Mapping) else None
        if not isinstance(limit, Mapping) or type(limit.get("output")) is not int or limit["output"] != output_budget["effective_max_output_tokens"]:
            raise ProviderNotConfiguredError("resolved Ollama output budget changed")
        context = output_budget["capability"]["context_length"]
        if context is not None and (type(limit.get("context")) is not int or limit["context"] != context):
            raise ProviderNotConfiguredError("resolved Ollama context changed")


class LocalOllamaReviewerAdapter:
    """Run a local Ollama reviewer with packet-only, read-only, no-tool isolation.

    Ollama is modelled as a local provider (``is_local=True``). The adapter reuses the
    exact packet-only execution path of the remote reviewer (fresh read-only temp dir,
    all tools denied, JSON-only protocol, timeout/UNKNOWN/step-limit semantics) but the
    cost is a confirmed local zero and the endpoint must be a deterministic loopback or
    the call fails closed. Before any inference the resolved OpenCode configuration is
    read (``opencode debug config --pure``) and the Ollama provider's base URL is
    verified to be a local loopback; a remote or unverifiable endpoint is rejected.
    It only serves ``review``/``rereview`` and enforces ``read_only=True``; it is never
    a valid implementation/revision target.
    """

    provider = "ollama"

    def __init__(
        self,
        *,
        planned_models: Sequence[ModelRef] = (),
        opencode_command: str | None = None,
        timeout_seconds: int = 900,
        discovery_timeout_seconds: int = 15,
        ollama_host: str | None = None,
        test_double: bool = False,
    ) -> None:
        for model in planned_models:
            if model.provider != self.provider or not model.is_local:
                raise ValueError(
                    "planned local Ollama models must be the ollama provider and marked local"
                )
        self.planned_models = tuple(planned_models)
        self.opencode_command = opencode_command or shutil.which("opencode") or "opencode"
        self.timeout_seconds = timeout_seconds
        self.discovery_timeout_seconds = discovery_timeout_seconds
        self.test_double = test_double
        self.adapter_id = "opencode-local-ollama-review"
        # The explicitly requested endpoint (constructor argument first, then the
        # OLLAMA_HOST environment). It is validated against the resolved OpenCode
        # config at call time, never trusted from this snapshot alone.
        self._ollama_host_raw = (
            ollama_host if ollama_host is not None else os.environ.get("OLLAMA_HOST")
        )

    def _discover_model_ids(self) -> tuple[str, ...]:
        return _discover_remote_model_ids(
            self.provider,
            self.opencode_command,
            discovery_timeout_seconds=self.discovery_timeout_seconds,
        )

    def _resolve_ollama_config(
        self,
        model_id: str | None,
        *,
        review_root: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Read and validate the resolved OpenCode config for the Ollama provider."""
        if review_root is not None:
            text = _read_resolved_config(
                self.opencode_command,
                review_root,
                self.discovery_timeout_seconds,
                environment=environment,
            )
            parsed = _parse_resolved_ollama_config(text, model_id=model_id)
            _validate_model_entry_endpoint(parsed["model_entry"], parsed["endpoint"])
            _validate_explicit_endpoint(self._ollama_host_raw, parsed["endpoint"])
            return parsed
        with tempfile.TemporaryDirectory(prefix="agentflow-ollama-config-") as directory:
            return self._resolve_ollama_config(
                model_id, review_root=Path(directory), environment=environment
            )

    def _prepare_local_environment(
        self, review_root: Path, environment: dict[str, str], model_id: str,
        *, steps: int = DEFAULT_REVIEW_MAX_STEPS,
        request: InvocationRequest | None = None,
    ) -> None:
        """Bind a verified loopback endpoint into the child process environment.

        Reads the resolved config, verifies the endpoint and the selected model's
        override, strips proxy variables, writes the verified endpoint and deny
        config into ``OPENCODE_CONFIG_CONTENT``, and finally re-reads the resolved
        config once more (at most two parses per invocation) to confirm a managed
        override did not silently change the endpoint or re-add a permission.
        """
        parsed = self._resolve_ollama_config(model_id, review_root=review_root)
        endpoint = parsed["endpoint"]
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            environment.pop(key, None)
        environment["NO_PROXY"] = "*"
        environment["no_proxy"] = "*"
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            _local_ollama_permission_config(
                model_id=model_id,
                endpoint=endpoint,
                model_entry=parsed["model_entry"],
                steps=steps,
            ),
            separators=(",", ":"),
        )
        second = self._resolve_ollama_config(
            model_id, review_root=review_root, environment=environment
        )
        _verify_local_config_consistency(second, endpoint, steps=steps)
        if request is not None:
            resolved = _prepare_output_environment(
                self.opencode_command, review_root, environment,
                request, validate_invocation_budgets(request),
            )
            final = _parse_resolved_ollama_config(canonical_json(resolved), model_id=model_id)
            _validate_model_entry_endpoint(final["model_entry"], endpoint)
            _validate_explicit_endpoint(self._ollama_host_raw, endpoint)
            _verify_local_config_consistency(final, endpoint, steps=steps,
                                            output_budget=validate_invocation_budgets(request))

    def discover(self) -> Sequence[ModelRecord]:
        # The resolved-config security validation runs outside the availability
        # try/except so a remote or invalid endpoint is never swallowed.
        self._resolve_ollama_config(None)
        try:
            discovered = set(self._discover_model_ids())
        except ReviewerUnavailableError:
            # Ollama is not reachable / has no configured models: report the true
            # state (unavailable) rather than auto-starting, downloading or raising.
            discovered = set()
        refs = self.planned_models or tuple(
            ModelRef(self.provider, model_id, model_id, None, True)
            for model_id in sorted(discovered)
        )
        return tuple(
            ModelRecord(
                ref=model,
                available=False,
                context_length=None,
                tool_capable=False,
                input_cost_per_million=0,
                output_cost_per_million=0,
                measured_tokens_per_second=None,
                trust_level=TrustLevel.UNVERIFIED,
                highest_allowed_risk=BusinessImportance.NORMAL,
                availability_state=(
                    ModelAvailabilityState.DISCOVERABLE
                    if model.model_id in discovered
                    else ModelAvailabilityState.UNAVAILABLE
                ),
            )
            for model in refs
        )

    def require_model(self, model: ModelRef) -> None:
        if model.provider != self.provider or not model.is_local:
            raise UnsupportedProviderError(
                "local Ollama reviewer only accepts local ollama models"
            )
        self._resolve_ollama_config(model.model_id)
        if model.model_id not in self._discover_model_ids():
            raise ModelUnavailableError(
                f"model unavailable: {self.provider}/{model.model_id}"
            )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        if request.role not in {"review", "rereview"}:
            raise ValueError(
                "local Ollama role denied: only review and rereview are allowed"
            )
        if not request.read_only:
            raise ValueError("local Ollama role denied: reviewer must be read-only")
        steps = _review_steps(request.metadata)
        validate_invocation_budgets(request)
        self.require_model(request.model)
        model_label = f"{self.provider}/{request.model.model_id}"
        command_prefix = (
            self.opencode_command,
            "run",
            "--format",
            "json",
            "--pure",
            "--auto",
            "--agent",
            "agentflow-local-ollama-reviewer",
            "--model",
            model_label,
        )
        return _run_opencode_packet_review(
            command_prefix=command_prefix,
            config_content=json.dumps(self._permission_config(steps=steps), separators=(",", ":")),
            prompt=request.prompt,
            timeout_seconds=self.timeout_seconds,
            is_local=True,
            review_label="local Ollama review",
            configured_steps=steps,
            model_label=model_label,
            on_provider_request_id=request.metadata.get("on_provider_request_id"),
            prepare_environment=lambda root, environment: self._prepare_local_environment(
                root, environment, request.model.model_id, steps=steps, request=request
            ),
        )

    def query(self, provider_request_id: str) -> InvocationResult | None:
        return None

    def cancel(self, provider_request_id: str) -> bool:
        return OpenCodeAdapter().cancel(provider_request_id)

    def _permission_config(self, *, steps: int = DEFAULT_REVIEW_MAX_STEPS) -> dict[str, Any]:
        permission = _reviewer_deny_permission()
        return {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": [self.provider],
            "permission": permission,
            "agent": {
                "agentflow-local-ollama-reviewer": {
                    "description": "Packet-only read-only local Ollama reviewer",
                    "mode": "primary",
                    "steps": steps,
                    "permission": permission,
                }
            },
        }


class RemoteOpenCodeWorkerAdapter:
    """Run an authorized remote implementation/revision worker with network denied."""

    def __init__(
        self,
        provider: str,
        *,
        planned_models: Sequence[ModelRef] = (),
        opencode_command: str | None = None,
        timeout_seconds: int = DEFAULT_REMOTE_WORKER_TIMEOUT_SECONDS,
        discovery_timeout_seconds: int = 15,
        test_double: bool = False,
    ) -> None:
        self.provider = validate_provider_id(provider)
        if self.provider in {"fake", "lmstudio"}:
            raise UnsupportedProviderError(
                f"unsupported provider for remote worker: {self.provider}"
            )
        for model in planned_models:
            if model.provider != self.provider or model.is_local:
                raise ValueError("planned remote models must match the adapter provider")
        self.planned_models = tuple(planned_models)
        self.opencode_command = opencode_command or shutil.which("opencode") or "opencode"
        self.timeout_seconds = timeout_seconds
        self.discovery_timeout_seconds = discovery_timeout_seconds
        self.test_double = test_double
        self.adapter_id = f"opencode-remote-worker:{self.provider}"

    def _discover_model_ids(self) -> tuple[str, ...]:
        return _discover_remote_model_ids(
            self.provider,
            self.opencode_command,
            discovery_timeout_seconds=self.discovery_timeout_seconds,
        )

    def discover(self) -> Sequence[ModelRecord]:
        discovered = set(self._discover_model_ids())
        refs = self.planned_models or tuple(
            ModelRef(self.provider, model_id, model_id, is_local=False)
            for model_id in sorted(discovered)
        )
        return tuple(
            ModelRecord(
                ref=model,
                available=False,
                context_length=None,
                tool_capable=False,
                input_cost_per_million=None,
                output_cost_per_million=None,
                measured_tokens_per_second=None,
                trust_level=TrustLevel.UNVERIFIED,
                highest_allowed_risk=BusinessImportance.NORMAL,
                availability_state=(
                    ModelAvailabilityState.DISCOVERABLE
                    if model.model_id in discovered
                    else ModelAvailabilityState.UNAVAILABLE
                ),
            )
            for model in refs
        )

    def require_model(self, model: ModelRef) -> None:
        if model.provider != self.provider or model.is_local:
            raise UnsupportedProviderError(
                f"unsupported provider/model locality: {model.provider}"
            )
        if model.model_id not in self._discover_model_ids():
            raise ModelUnavailableError(
                f"model unavailable: {self.provider}/{model.model_id}"
            )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        if request.role not in {"implementation", "revision"}:
            raise ValueError("remote role denied: only implementation and revision are allowed")
        if request.read_only:
            raise ValueError("remote role denied: implementation must be a write role")
        output_budget = validate_invocation_budgets(request)
        self.require_model(request.model)
        worktree = Path(str(request.metadata["worktree"])).resolve()
        if not worktree.is_dir():
            raise ValueError("invocation worktree does not exist")
        steps = _remote_worker_steps(request.metadata)
        timeout_seconds = _remote_worker_timeout(
            request.metadata, default=self.timeout_seconds
        )
        environment = os.environ.copy()
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            self._permission_config(steps=steps), separators=(",", ":")
        )
        _prepare_output_environment(self.opencode_command, worktree, environment, request, output_budget)
        command = (
            self.opencode_command,
            "run",
            "--format",
            "json",
            "--pure",
            "--auto",
            "--agent",
            "agentflow-remote-worker",
            "--model",
            f"{self.provider}/{request.model.model_id}",
            "--dir",
            str(worktree),
            OpenCodeAdapter._bounded_prompt(request),
        )
        started = time.monotonic()
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        process_reference = f"opencode-process-group:{process.pid}"
        callback = request.metadata.get("on_provider_request_id")
        if callable(callback):
            callback(process_reference)
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            OpenCodeAdapter().cancel(process_reference)
            partial_stdout, cleanup_incomplete = _collect_timeout_output(error, process)
            duration_ms = int((time.monotonic() - started) * 1000)
            result = parse_opencode_partial_usage(
                partial_stdout,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
                cost_unavailable=True,
                cleanup_incomplete=cleanup_incomplete,
            )
            raise InvocationOutcomeUnknown(
                "remote OpenCode worker timed out; no result is available",
                process_reference,
                result=result,
                termination_reason="timeout",
            ) from error
        except (KeyboardInterrupt, OSError) as error:
            OpenCodeAdapter().cancel(process_reference)
            _bounded_drain(process)
            raise InvocationOutcomeUnknown(
                "invocation outcome unknown: remote OpenCode worker was interrupted",
                process_reference,
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if process.returncode is not None and process.returncode < 0:
            raise InvocationOutcomeUnknown(
                "invocation outcome unknown: remote OpenCode worker was terminated",
                process_reference,
                termination_reason="signal_terminated",
            )
        if process.returncode:
            try:
                parse_opencode_json(
                    stdout,
                    duration_ms=duration_ms,
                    is_local=False,
                    configured_step_limit=steps,
                )
            except InvocationIncompleteError:
                raise
            except RuntimeError:
                pass
            failed_usage = parse_opencode_failed_usage(
                _as_output_bytes(stdout),
                duration_ms=duration_ms,
                is_local=False,
                termination_reason="nonzero_exit",
            )
            raise WorkerProtocolError(
                stderr.strip()
                or f"remote worker failed: {self.provider}/{request.model.model_id}",
                result=failed_usage,
            )
        try:
            return parse_opencode_json(
                stdout,
                duration_ms=duration_ms,
                is_local=False,
                configured_step_limit=steps,
            )
        except InvocationIncompleteError:
            raise
        except RuntimeError as error:
            failed_usage = parse_opencode_failed_usage(
                _as_output_bytes(stdout),
                duration_ms=duration_ms,
                is_local=False,
                termination_reason="no_usable_result",
            )
            raise WorkerProtocolError(
                "remote worker returned no usable result", result=failed_usage
            ) from error

    def query(self, provider_request_id: str) -> InvocationResult | None:
        return None

    def cancel(self, provider_request_id: str) -> bool:
        return OpenCodeAdapter().cancel(provider_request_id)

    def _permission_config(self, *, steps: int) -> dict[str, Any]:
        permission = {
            "*": "deny",
            "read": {
                "*": "allow",
                ".env": "deny",
                ".env.*": "deny",
                "**/.env": "deny",
                "**/.env.*": "deny",
            },
            "glob": "allow",
            "grep": "allow",
            "edit": "allow",
            "write": "allow",
            "bash": "deny",
            "shell": "deny",
            "external_directory": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "task": "deny",
            "subagent": "deny",
            "skill": "deny",
            "question": "deny",
        }
        return {
            "$schema": "https://opencode.ai/config.json",
            "enabled_providers": [self.provider],
            "permission": permission,
            "agent": {
                "agentflow-remote-worker": {
                    "description": "Bounded remote AgentFlow worker",
                    "mode": "primary",
                    "steps": steps,
                    "permission": permission,
                }
            },
        }


def _selected_output_model(config: Mapping[str, Any], request: InvocationRequest) -> tuple[dict, dict]:
    try:
        provider = config["provider"][request.model.provider]
        model = provider["models"][request.model.model_id]
    except (KeyError, TypeError) as error:
        raise ProviderNotConfiguredError("selected output model configuration is missing") from error
    if not isinstance(provider, dict) or not isinstance(model, dict):
        raise ProviderNotConfiguredError("selected output configuration is invalid")
    return provider, model


def _reject_output_overrides(value: Any) -> None:
    # Provider options can override generation arguments after the generic limit.
    # Unknown token/thinking overrides cannot be proven within the authorization.
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).lower().replace("_", "")
            if ((normalized.startswith("max") and "token" in normalized)
                    or any(word in normalized for word in ("maxoutput", "numpredict", "budgettoken", "thinking"))):
                label = re.sub(r"[^a-zA-Z0-9_.-]", "?", str(key))[:64]
                raise ProviderNotConfiguredError(
                    f"unverified provider output/reasoning override: {label}; "
                    "including disabled thinking options; values are not logged"
                )
            _reject_output_overrides(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_output_overrides(item)


def _read_output_config(command: str, root: Path, timeout: int, *, environment: dict[str, str]) -> str:
    try:
        version = subprocess.run(
            (command, "--version"), cwd=str(root), capture_output=True, text=True,
            check=False, timeout=timeout, env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderNotConfiguredError("OpenCode output-control version cannot be verified") from error
    found = version.stdout.strip()
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", found) if len(found) <= 64 else None
    compatible = (
        not version.returncode
        and match is not None
        and (int(match.group(1)), int(match.group(2))) == (1, 18)
        and int(match.group(3)) >= 29
    )
    if not compatible:
        if version.returncode or match is None or len(found) > 64:
            found = "unknown or invalid"
        raise ProviderNotConfiguredError(
            "OpenCode output-control version is incompatible: "
            f"supported=1.18.29+ within 1.18.x, found={found}"
        )
    return _read_resolved_config(command, root, timeout, environment=environment)


def _prepare_output_environment(command: str, root: Path, environment: dict[str, str],
                                request: InvocationRequest, budget: Mapping[str, Any]) -> dict[str, Any]:
    """Bind both OpenCode limits and verify the resolved child config without logging it."""
    overlay = json.loads(environment["OPENCODE_CONFIG_CONTENT"])
    try:
        base = json.loads(_read_output_config(command, root, 15, environment=environment))
    except (ValueError, TypeError) as error:
        raise ProviderNotConfiguredError("output configuration cannot be resolved") from error
    provider, model = _selected_output_model(base, request)
    # This transport's final max_tokens mapping was verified in OpenCode 1.18.29.
    # Other SDKs may add reasoning tokens or override generation arguments; do not
    # claim enforcement for an unverified transport merely because it accepts JSON.
    if (provider.get("npm") != "@ai-sdk/openai-compatible"
            or model.get("npm", provider["npm"]) != provider["npm"]):
        raise ProviderNotConfiguredError("output limit enforcement is unverified for this transport")
    expected_api_id = budget["capability"].get("api_model_id") or request.model.model_id
    if model.get("id", request.model.model_id) != expected_api_id:
        raise ProviderNotConfiguredError("model API alias differs from frozen capability")
    _reject_output_overrides(provider.get("options", {}))
    _reject_output_overrides(model.get("options", {}))
    _reject_output_overrides(model.get("variants", {}))
    agents = base.get("agent", {})
    if not isinstance(agents, Mapping):
        raise ProviderNotConfiguredError("invalid resolved agents")
    for agent in agents.values():
        _reject_output_overrides(agent.get("options", {}) if isinstance(agent, dict) else {})
    limit = model.get("limit", {})
    if not isinstance(limit, dict):
        raise ProviderNotConfiguredError("invalid resolved model limits")
    # Explicit registry evidence may fill an unknown catalog output, but a smaller
    # currently declared capability is never silently ignored.
    current = limit.get("output")
    effective = budget["effective_max_output_tokens"]
    if current is not None and (type(current) is not int or current < budget["capability"]["max_output_tokens"]):
        raise ProviderNotConfiguredError("model output capability decreased or is invalid")
    if current is None and budget["capability"]["source"] == "opencode_catalog":
        raise ProviderNotConfiguredError("catalog output capability source is unavailable")
    context = budget["capability"]["context_length"]
    if "context" in limit and (type(limit["context"]) is not int or limit["context"] <= 0):
        raise ProviderNotConfiguredError("invalid resolved model context")
    if context is not None and limit.get("context", context) < context:
        raise ProviderNotConfiguredError("model context capability decreased")
    selected = deepcopy(model)
    selected["limit"] = {**limit, "output": effective}
    if context is not None:
        selected["limit"]["context"] = context
    overlay.setdefault("provider", {}).setdefault(request.model.provider, {}).setdefault("models", {})[request.model.model_id] = selected
    environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(overlay, separators=(",", ":"))
    environment["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = str(effective)
    try:
        resolved = json.loads(_read_output_config(command, root, 15, environment=environment))
    except (ValueError, TypeError) as error:
        raise ProviderNotConfiguredError("output configuration cannot be verified") from error
    final_provider, final_model = _selected_output_model(resolved, request)
    if canonical_json(final_model) != canonical_json(selected):
        raise ProviderNotConfiguredError("resolved output model configuration changed")
    for key in ("npm", "options"):
        if final_provider.get(key) != provider.get(key):
            raise ProviderNotConfiguredError("resolved provider transport/options changed")
    if resolved.get("enabled_providers") != overlay["enabled_providers"]:
        raise ProviderNotConfiguredError("resolved provider scope changed")
    if request.role in ("review", "rereview"):
        _verify_deny_permission(resolved.get("permission"), "global permission")
    elif resolved.get("permission") != overlay["permission"]:
        raise ProviderNotConfiguredError("resolved permissions changed")
    final_agents = resolved.get("agent", {})
    if not isinstance(final_agents, Mapping):
        raise ProviderNotConfiguredError("invalid resolved agents")
    expected_model = f"{request.model.provider}/{request.model.model_id}"
    for name, expected in overlay["agent"].items():
        actual = final_agents.get(name, {})
        if not isinstance(actual, Mapping):
            raise ProviderNotConfiguredError("invalid resolved agent")
        if actual.get("model", expected_model) != expected_model:
            raise ProviderNotConfiguredError("resolved agent model changed")
        _reject_output_overrides(actual)
        if (type(actual.get("steps")) is not int or actual["steps"] != expected["steps"]
                or actual.get("permission") != expected["permission"]):
            raise ProviderNotConfiguredError("resolved agent budget/permissions changed")
    return resolved


def _review_steps(metadata: Mapping[str, Any]) -> int:
    value = metadata.get("review_max_steps", DEFAULT_REVIEW_MAX_STEPS)
    if type(value) is not int or not 2 <= value <= REVIEW_MAX_STEPS_LIMIT:
        raise ValueError("review_max_steps must be an integer between 2 and 32")
    return value


def _implementation_steps(metadata: Mapping[str, Any]) -> int:
    value = metadata.get("implementation_max_steps", DEFAULT_IMPLEMENTATION_MAX_STEPS)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("implementation_max_steps must be a positive integer")
    if not 1 <= value <= IMPLEMENTATION_MAX_STEPS_LIMIT:
        raise ValueError(
            "implementation_max_steps must be between 1 and "
            f"{IMPLEMENTATION_MAX_STEPS_LIMIT}"
        )
    return value


def _remote_worker_steps(metadata: Mapping[str, Any]) -> int:
    value = metadata.get("remote_worker_max_steps", DEFAULT_REMOTE_WORKER_MAX_STEPS)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("remote_worker_max_steps must be a positive integer")
    if not 1 <= value <= REMOTE_WORKER_MAX_STEPS_LIMIT:
        raise ValueError(
            "remote_worker_max_steps must be between 1 and "
            f"{REMOTE_WORKER_MAX_STEPS_LIMIT}"
        )
    return value


def _remote_worker_timeout(metadata: Mapping[str, Any], *, default: int) -> int:
    value = metadata.get("remote_worker_timeout_seconds", default)
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, int):
        raise ValueError("remote_worker_timeout_seconds must be an integer")
    if not REMOTE_WORKER_TIMEOUT_SECONDS_MIN <= value <= REMOTE_WORKER_TIMEOUT_SECONDS_LIMIT:
        raise ValueError(
            "remote_worker_timeout_seconds must be between "
            f"{REMOTE_WORKER_TIMEOUT_SECONDS_MIN} and "
            f"{REMOTE_WORKER_TIMEOUT_SECONDS_LIMIT}"
        )
    return value


def _implementation_timeout(metadata: Mapping[str, Any]) -> int:
    value = metadata.get(
        "implementation_timeout_seconds", DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS
    )
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, int):
        raise ValueError("implementation_timeout_seconds must be an integer")
    if not IMPLEMENTATION_TIMEOUT_SECONDS_MIN <= value <= IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT:
        raise ValueError(
            "implementation_timeout_seconds must be between "
            f"{IMPLEMENTATION_TIMEOUT_SECONDS_MIN} and "
            f"{IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT}"
        )
    return value


def _continuation_session(metadata: Mapping[str, Any]) -> str | None:
    """Read and validate the reused OpenCode session ID for a continuation segment."""
    value = metadata.get("continuation_session_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("continuation_session_id must be a non-empty string")
    session_id = value.strip()
    if any(ch.isspace() or ord(ch) < 0x20 for ch in session_id):
        raise ValueError("continuation_session_id contains invalid characters")
    return session_id


def _verify_session_reuse(continuation_session_id: str, result: InvocationResult) -> None:
    """Reject a continuation whose returned session differs from the requested one.

    OpenCode is expected to report the same session ID it was asked to resume. A
    different (or missing) session means the continuation did not actually reuse
    the parent context, which is a protocol error that must not be silently
    continued, tested, or reviewed.
    """
    if result.provider_request_id == continuation_session_id:
        return
    raise InvocationIncompleteError(
        "OpenCode did not reuse the requested continuation session; the returned "
        "session ID differs from the requested session",
        result,
        failure_kind="session_mismatch",
    )


def _as_output_bytes(value: Any) -> bytes:
    if value is None:
        return b""
    if isinstance(value, bytes):
        return value
    return str(value).encode("utf-8")


def _merge_overlapping_output_bytes(first: bytes, second: bytes) -> bytes:
    """Join two byte reads of the same stream, removing the overlapping prefix.

    This runs on raw bytes so a UTF-8 multi-byte character split across the two
    reads still merges correctly, and identical-but-independent events are never
    treated as duplicates.
    """
    if not first:
        return second
    if not second:
        return first
    if second.startswith(first):
        return second
    if first.startswith(second):
        return first
    max_overlap = min(len(first), len(second))
    for size in range(max_overlap, 0, -1):
        if first.endswith(second[:size]):
            return first + second[size:]
    return first + second


_DRAIN_GRACE_SECONDS = 5.0
_DRAIN_KILL_GRACE_SECONDS = 2.0


def _kill_process_group(process: subprocess.Popen) -> None:
    """SIGKILL the process group after SIGTERM was ignored, best-effort.

    A process that ignores SIGTERM must be reaped so the caller never blocks
    indefinitely. SIGKILL targets the whole process group; if that is not
    available the process itself is killed directly.
    """
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except Exception:
            pass


def _bounded_drain(
    process: subprocess.Popen, *, partial: bytes = b""
) -> tuple[bytes, bool]:
    """Drain stdout after cancellation within a finite grace, escalating to kill.

    ``partial`` is output already captured before cancellation. Every wait is
    bounded: the first drain ``communicate`` uses the grace period, and after a
    best-effort SIGKILL the final reap uses a second, shorter bound. If that
    final wait also times out (a descendant still holds the pipe, or the kill
    failed) the already-captured bytes are returned together with a
    ``cleanup_incomplete`` diagnostic, never a fabricated "process exited" claim.
    The caller preserves UNKNOWN diagnostics and captured usage from the bytes.
    """
    merged = partial
    cleanup_incomplete = False
    try:
        second_stdout, _ = process.communicate(timeout=_DRAIN_GRACE_SECONDS)
        merged = _merge_overlapping_output_bytes(
            merged, _as_output_bytes(second_stdout)
        )
    except subprocess.TimeoutExpired as drain_error:
        merged = _merge_overlapping_output_bytes(
            merged, _as_output_bytes(drain_error.output)
        )
        _kill_process_group(process)
        try:
            third_stdout, _ = process.communicate(timeout=_DRAIN_KILL_GRACE_SECONDS)
            merged = _merge_overlapping_output_bytes(
                merged, _as_output_bytes(third_stdout)
            )
        except subprocess.TimeoutExpired as final_error:
            merged = _merge_overlapping_output_bytes(
                merged, _as_output_bytes(final_error.output)
            )
            cleanup_incomplete = True
        except Exception:
            cleanup_incomplete = True
    except Exception:
        pass
    return merged, cleanup_incomplete


def _collect_timeout_output(
    error: subprocess.TimeoutExpired, process: subprocess.Popen
) -> tuple[bytes, bool]:
    return _bounded_drain(process, partial=_as_output_bytes(error.output))


def _valid_cost_value(value: object) -> float | None:
    """Return a finite non-negative cost value, or ``None`` when invalid.

    Only ``int``/``float`` (never ``bool``) counts are accepted. Negative, NaN,
    infinite, and non-numeric values (including numeric strings) are invalid and
    return ``None`` so the caller can mark the cost unavailable instead of
    fabricating a settled amount.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _scan_usage_events(data: bytes) -> dict[str, object]:
    """Sum confirmed token/cost/session evidence from a partial OpenCode stream.

    Every token- or cost-bearing event is summed; events are never de-duplicated
    by content. Invalid cost values (negative, NaN, infinite, bool, string) are
    never treated as a valid amount and instead mark the whole stream's cost as
    invalid, so a mixed valid/invalid stream cannot masquerade as a complete
    settled cost. Returns the accumulated usage plus the first session id.
    """
    text = data.decode("utf-8", errors="replace")
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    saw_reasoning = False
    saw_input = False
    saw_output = False
    usage_incomplete = False
    provider_request_id: str | None = None
    event_count = 0
    completed_step_count = 0
    reported_cost = 0.0
    cost_reported = False
    cost_invalid = False

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        event_count += 1
        provider_request_id = provider_request_id or _find_string(
            event, ("sessionID", "sessionId", "session_id")
        )
        if str(event.get("type", "")) == "step_finish":
            completed_step_count += 1
        part = event.get("part") if isinstance(event.get("part"), Mapping) else event
        tokens_raw = part.get("tokens")
        tokens = tokens_raw if isinstance(tokens_raw, Mapping) else {}
        in_tokens = _valid_token_value(tokens.get("input"))
        out_tokens = _valid_token_value(tokens.get("output"))
        if "input" in tokens and in_tokens is not None:
            saw_input = True
            input_tokens += in_tokens
        if "output" in tokens and out_tokens is not None:
            saw_output = True
            output_tokens += out_tokens
        if isinstance(tokens_raw, Mapping) or str(event.get("type", "")) == "step_finish":
            if "input" not in tokens or in_tokens is None:
                usage_incomplete = True
            if "output" not in tokens or out_tokens is None:
                usage_incomplete = True
        reasoning = _valid_token_value(tokens.get("reasoning"))
        if reasoning is not None and reasoning > 0:
            saw_reasoning = True
            reasoning_tokens += reasoning
        cost = part.get("cost", event.get("cost"))
        if cost is not None:
            valid = _valid_cost_value(cost)
            if valid is None:
                cost_invalid = True
            else:
                reported_cost += valid
                if not math.isfinite(reported_cost):
                    cost_invalid = True
                else:
                    cost_reported = True

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "saw_reasoning": saw_reasoning,
        "provider_request_id": provider_request_id,
        "event_count": event_count,
        "completed_step_count": completed_step_count,
        "saw_usage": (saw_input and saw_output) and not usage_incomplete,
        "reported_cost": reported_cost,
        "cost_reported": cost_reported,
        "cost_invalid": cost_invalid,
    }


def parse_opencode_partial_usage(
    data: bytes,
    *,
    duration_ms: int,
    timeout_seconds: int | None = None,
    cost_unavailable: bool = False,
    cleanup_incomplete: bool = False,
    termination_reason: str = "timeout",
) -> InvocationResult:
    """Extract conservative usage evidence from partial OpenCode JSON events.

    The output text is intentionally empty: a timed-out or otherwise interrupted
    call has no confirmed result. Each completed step reports its own token usage,
    so every real token-bearing event in the merged stream is summed. Overlap
    between the two reads is removed at the raw byte-stream level before decoding;
    events are never de-duplicated by their content. ``termination_reason`` records
    the precise interruption cause (``timeout``, ``signal_terminated``,
    ``interrupted`` or ``communication_error``); ``timeout_seconds`` is only
    attached for a timeout. For remote calls ``cost_unavailable`` marks the remote
    cost as unavailable rather than reporting a fabricated zero.
    """
    scan = _scan_usage_events(data)
    metadata: dict[str, Any] = {
        "termination_reason": termination_reason,
        "token_source": (
            "opencode_json_events" if scan["saw_usage"] else "unavailable"
        ),
        "usage_unavailable": not scan["saw_usage"],
        "event_count": scan["event_count"],
        "completed_step_count": scan["completed_step_count"],
        "session_id": scan["provider_request_id"],
        "reasoning_tokens": (
            scan["reasoning_tokens"] if scan["saw_reasoning"] else None
        ),
        "partial_stdout_bytes": len(data),
        "partial_stdout_sha256": hashlib.sha256(data).hexdigest(),
        "cleanup_incomplete": cleanup_incomplete,
        "cost_invalid": scan["cost_invalid"],
    }
    if timeout_seconds is not None:
        metadata["timeout_seconds"] = timeout_seconds
    return InvocationResult(
        provider_request_id=scan["provider_request_id"],
        output="",
        input_tokens=scan["input_tokens"],
        output_tokens=scan["output_tokens"],
        first_token_latency_ms=None,
        duration_ms=duration_ms,
        remote_cost=None if cost_unavailable else 0.0,
        raw_metadata=metadata,
        cost_unavailable=cost_unavailable,
    )


def parse_opencode_failed_usage(
    data: bytes,
    *,
    duration_ms: int,
    is_local: bool,
    termination_reason: str,
) -> InvocationResult:
    """Extract confirmed usage from a post-call failure's stdout.

    A process that exited non-zero (or returned no usable body) still reported
    real token and cost evidence on ``step_finish`` events. That evidence is
    confirmed, not fabricated, so it must be preserved even though the call
    failed. With no cost evidence the remote cost stays ``None`` (unavailable)
    instead of reporting a false zero.
    """
    scan = _scan_usage_events(data)
    cost_usable = scan["cost_reported"] and not scan["cost_invalid"]
    metadata = {
        "termination_reason": termination_reason,
        "token_source": (
            "opencode_json_events" if scan["saw_usage"] else "unavailable"
        ),
        "usage_unavailable": not scan["saw_usage"],
        "event_count": scan["event_count"],
        "completed_step_count": scan["completed_step_count"],
        "session_id": scan["provider_request_id"],
        "reasoning_tokens": (
            scan["reasoning_tokens"] if scan["saw_reasoning"] else None
        ),
        "reported_cost": scan["reported_cost"] if cost_usable else None,
        "cost_invalid": scan["cost_invalid"],
        "cost_unavailable": not is_local and not cost_usable,
        "stdout_bytes": len(data),
        "stdout_sha256": hashlib.sha256(data).hexdigest(),
    }
    return InvocationResult(
        provider_request_id=scan["provider_request_id"],
        output="",
        input_tokens=scan["input_tokens"],
        output_tokens=scan["output_tokens"],
        first_token_latency_ms=None,
        duration_ms=duration_ms,
        remote_cost=(
            0.0 if is_local else (scan["reported_cost"] if cost_usable else None)
        ),
        raw_metadata=metadata,
        cost_unavailable=not is_local and not cost_usable,
    )


def parse_opencode_json(
    text: str,
    *,
    duration_ms: int,
    is_local: bool = False,
    configured_step_limit: int | None = None,
) -> InvocationResult:
    output_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    saw_reasoning = False
    provider_request_id: str | None = None
    first_token_latency_ms: int | None = None
    reported_cost = 0.0
    cost_reported = False
    cost_invalid = False
    event_types: list[str] = []
    structured_step_limit = False
    step_start_count = 0
    step_finish_count = 0
    tool_use_count = 0
    saw_input = False
    saw_output = False
    usage_incomplete = False
    # The reason of the last step_finish event that carries one. A stream may
    # contain several step_finish events, so "terminal" means the final defined
    # reason, never the first intermediate one.
    terminal_reason: str | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("type", "unknown"))
        event_types.append(event_type)
        if event_type == "step_start":
            step_start_count += 1
        elif event_type == "step_finish":
            step_finish_count += 1
        elif event_type == "tool_use":
            tool_use_count += 1
        structured_step_limit = structured_step_limit or _event_reports_step_limit(
            event
        )
        provider_request_id = provider_request_id or _find_string(
            event, ("sessionID", "sessionId", "session_id")
        )
        part = event.get("part") if isinstance(event.get("part"), Mapping) else event
        if part.get("type") == "text" and part.get("text"):
            output_parts.append(str(part["text"]))
            if first_token_latency_ms is None:
                first_token_latency_ms = _find_int(
                    part, ("firstTokenLatencyMs", "latency")
                )
        if event_type == "step_finish":
            reason = _find_string(
                part, ("reason", "finishReason", "finish_reason", "finishreason")
            )
            if reason is not None:
                terminal_reason = reason
        tokens = part.get("tokens") if isinstance(part.get("tokens"), Mapping) else {}
        in_tokens = _valid_token_value(tokens.get("input"))
        out_tokens = _valid_token_value(tokens.get("output"))
        # A token key with a valid (non-bool, non-negative) value -- including an
        # explicit zero -- is confirmed usage. A missing key or an invalid value
        # (string, null, bool, negative) is not credited and leaves that side unknown.
        if "input" in tokens and in_tokens is not None:
            saw_input = True
            input_tokens += in_tokens
        if "output" in tokens and out_tokens is not None:
            saw_output = True
            output_tokens += out_tokens
        # Any token-carrying event (or a step_finish, which reports usage) with a
        # missing or invalid input/output keeps the whole call's usage unavailable;
        # a later valid event must not clear this.
        if isinstance(part.get("tokens"), Mapping) or event_type == "step_finish":
            if "input" not in tokens or in_tokens is None:
                usage_incomplete = True
            if "output" not in tokens or out_tokens is None:
                usage_incomplete = True
        reasoning = _valid_token_value(tokens.get("reasoning"))
        if reasoning is not None and reasoning > 0:
            saw_reasoning = True
            reasoning_tokens += reasoning
        cost = part.get("cost", event.get("cost"))
        if cost is not None:
            valid = _valid_cost_value(cost)
            if valid is None:
                cost_invalid = True
            else:
                reported_cost += valid
                if not math.isfinite(reported_cost):
                    cost_invalid = True
                else:
                    cost_reported = True

    output = "".join(output_parts).strip()
    text_status, matched_rule_id, matched_line = (
        _text_step_limit_signal(output_parts) if output_parts else (None, None, None)
    )
    if terminal_reason == "length":
        termination_source = "structured_event"
        failure_kind = "output_limit_reached"
    elif structured_step_limit:
        termination_source = "structured_event"
        failure_kind = "step_limit_reached"
    elif text_status == "confirmed":
        termination_source = "final_text"
        failure_kind = "step_limit_reached"
    elif text_status == "suspected":
        termination_source = "final_text"
        failure_kind = "suspected_step_limit"
    else:
        termination_source = None
        failure_kind = None

    cost_usable = cost_reported and not cost_invalid
    metadata: dict[str, Any] = {
        "event_types": event_types,
        "reported_cost": reported_cost if cost_usable else None,
        "cost_invalid": cost_invalid,
        "cost_unavailable": not is_local and not cost_usable,
        "reasoning_tokens": reasoning_tokens if saw_reasoning else None,
        "classifier_version": _CLASSIFIER_VERSION,
        "step_start_count": step_start_count,
        "step_finish_count": step_finish_count,
        "tool_use_count": tool_use_count,
        "session_id": provider_request_id,
        "token_source": "opencode_json_events" if ((saw_input and saw_output) and not usage_incomplete) else "unavailable",
        "usage_unavailable": not ((saw_input and saw_output) and not usage_incomplete),
    }
    if configured_step_limit is not None:
        metadata["configured_step_limit"] = configured_step_limit
    if terminal_reason is not None:
        metadata["terminal_reason"] = terminal_reason
    if termination_source is not None:
        metadata["failure_kind"] = failure_kind
        metadata["termination_source"] = termination_source
        if matched_rule_id is not None:
            metadata["matched_rule_id"] = matched_rule_id
        if matched_line is not None:
            metadata["matched_line"] = matched_line

    result = InvocationResult(
        provider_request_id=provider_request_id,
        output=output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        first_token_latency_ms=first_token_latency_ms,
        duration_ms=duration_ms,
        remote_cost=0.0 if is_local else (reported_cost if cost_usable else None),
        raw_metadata=metadata,
        cost_unavailable=not is_local and not cost_usable,
    )
    if failure_kind == "output_limit_reached":
        raise InvocationIncompleteError(
            "OpenCode reached its output limit before completing the invocation",
            result, failure_kind="output_limit_reached",
        )
    if failure_kind == "step_limit_reached":
        raise InvocationIncompleteError(
            "OpenCode reached its maximum step limit before completing the invocation",
            result,
            failure_kind="step_limit_reached",
        )
    if failure_kind == "suspected_step_limit":
        raise InvocationIncompleteError(
            "OpenCode emitted an unrecognized step-limit suffix; the invocation "
            "was not confirmed complete",
            result,
            failure_kind="suspected_step_limit",
        )
    if not output:
        metadata["termination_reason"] = "no_text"
        raise InvocationProtocolError("OpenCode returned no text event", result=result)
    return result


def _event_reports_step_limit(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    if isinstance(event_type, str) and _structured_value_reports_step_limit(
        event_type
    ):
        return True
    for key, value in event.items():
        normalized_key = re.sub(r"[^a-z]", "", str(key).lower())
        if normalized_key in {
            "code",
            "error",
            "finish",
            "finishreason",
            "kind",
            "message",
            "reason",
            "status",
            "termination",
            "terminationreason",
        }:
            if isinstance(value, str) and _structured_value_reports_step_limit(
                value
            ):
                return True
            if isinstance(value, Mapping) and _event_reports_step_limit(value):
                return True
        elif isinstance(value, Mapping) and _event_reports_step_limit(value):
            return True
    return False


def _structured_value_reports_step_limit(text: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    if normalized in {
        "max steps",
        "max steps reached",
        "maximum steps",
        "maximum steps reached",
        "step limit",
        "step limit reached",
        "steps exhausted",
    }:
        return True
    return _text_reports_step_limit(text)


# Shared marker grammar. Every step-limit classifier below derives from this one
# body pattern, so there is no second, drifting definition of a legal bare
# marker. The body deliberately excludes the optional trailing period: boundary
# and suffix rules are applied explicitly against the matched body instead of
# stripping punctuation first.
_STEP_LIMIT_MARKER_BODY = (
    r"(?:the )?max(?:imum)?(?: number of)? steps(?: for this agent)? "
    r"(?:have been|has been|were|was) reached"
    r"|critical\s*[-–—]\s*max(?:imum)? steps reached"
)

_STEP_LIMIT_LINE_RE = re.compile(
    rf"(?:{_STEP_LIMIT_MARKER_BODY})\.?", re.IGNORECASE
)

_STEP_LIMIT_MARKER_BODY_RE = re.compile(
    rf"(?:{_STEP_LIMIT_MARKER_BODY})", re.IGNORECASE
)

# Bumped whenever the termination classifier changes shape. Recorded in the
# invocation metadata so a stored result can be traced back to the rules that
# produced it.
_CLASSIFIER_VERSION = "3"

# Real, verified whole lines observed from OpenCode long tasks. Each maps the
# exact lowercased line to a stable classifier rule ID. Only these two lines may
# combine the bare marker with a summary lead-in on the same line; any other
# suffix is not a high-confidence step-limit signal.
_STEP_LIMIT_SUMMARY_LINES: tuple[tuple[str, str], ...] = (
    (
        "maximum steps for this agent have been reached. here is a summary of the work done and remaining tasks:",
        "step_limit_with_work_remaining_summary",
    ),
    (
        "maximum steps for this agent have been reached. here's a summary of the work completed and remaining:",
        "step_limit_with_work_completed_summary",
    ),
)


def _line_is_step_limit_marker(line: str) -> bool:
    """Match only a bare, unquoted step-limit termination line.

    The raw line is matched with strict anchoring, never after stripping
    punctuation, so Markdown headings, emphasis, inline code, list bullets and
    quotation marks all fail the match instead of being collapsed into a legal
    marker. Only a single trailing period is tolerated.
    """
    stripped = line.strip()
    if not stripped:
        return False
    return _STEP_LIMIT_LINE_RE.fullmatch(stripped) is not None


def _bare_marker_suffix(lowered: str) -> str | None:
    """Return the non-allowlisted suffix of a legal bare marker, or ``None``.

    ``lowered`` is a lowercased, stripped line. The line starts with a legal
    bare marker body from the same grammar as ``_STEP_LIMIT_LINE_RE``. A marker
    body must be followed by an explicit boundary: end of string, the single
    terminating period the bare marker allows, or whitespace/other punctuation
    separating a suffix. When the body is immediately followed by a letter,
    digit or underscore the "marker" is really a longer word (for example
    ``reachedness``) and is not a step-limit signal at all.
    """
    match = _STEP_LIMIT_MARKER_BODY_RE.match(lowered)
    if match is None:
        return None
    rest = lowered[match.end():]
    if not rest:
        return None
    first = rest[0]
    if first.isalnum() or first == "_":
        return None
    if first == "." and not rest[1:]:
        return None
    return rest


def _classify_step_limit_line(stripped: str) -> tuple[str | None, str | None]:
    """Classify one structurally-excluded candidate line.

    Returns ``(status, rule_id)`` where ``status`` is one of:

    - ``"confirmed"``: a legal bare marker or one of the two verified summary
      lead-ins. ``rule_id`` names the matched rule.
    - ``"suspected"``: the line starts with a legal bare marker body but carries
      an unrecognized suffix. ``rule_id`` is ``None``.
    - ``None``: the line does not signal a step limit.
    """
    if _line_is_step_limit_marker(stripped):
        return "confirmed", "bare_step_limit_marker"
    lowered = stripped.lower()
    for expected, rule_id in _STEP_LIMIT_SUMMARY_LINES:
        if lowered == expected:
            return "confirmed", rule_id
    if _bare_marker_suffix(lowered) is not None:
        return "suspected", None
    return None, None


def _scan_step_limit_lines(text: str) -> tuple[str | None, str | None, str | None]:
    """Scan text lines for a step-limit signal, preserving structural exclusions.

    Returns ``(status, rule_id, matched_line)``. A confirmed match takes
    priority over a suspected one; if no confirmed line is found, the first
    suspected line is reported instead.
    """
    in_fence = False
    suspected_line: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        stripped = raw_line.strip()
        if raw_line == raw_line.lstrip() and stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if raw_line != raw_line.lstrip():
            continue
        if stripped.startswith((">", "+", "-", "@@")):
            continue
        status, rule_id = _classify_step_limit_line(stripped)
        if status == "confirmed":
            return "confirmed", rule_id, stripped
        if status == "suspected" and suspected_line is None:
            suspected_line = stripped
    if suspected_line is not None:
        return "suspected", "unrecognized_step_limit_suffix", suspected_line
    return None, None, None


def _text_step_limit_signal(
    text_blocks: Sequence[str],
) -> tuple[str | None, str | None, str | None]:
    """Classify the step-limit signal across individual text events.

    Each text event is scanned independently so a marker split mid-line across
    two unrelated events is never assembled into a false match.
    """
    suspected_line: str | None = None
    for block in text_blocks:
        status, rule_id, line = _scan_step_limit_lines(block)
        if status == "confirmed":
            return "confirmed", rule_id, line
        if status == "suspected" and suspected_line is None:
            suspected_line = line
    if suspected_line is not None:
        return "suspected", "unrecognized_step_limit_suffix", suspected_line
    return None, None, None


def _text_reports_step_limit(text: str) -> bool:
    """Detect an independent, unquoted step-limit termination line.

    OpenCode appends its termination notice to an otherwise ordinary model turn,
    so the marker is often neither the first line nor the only line: it follows
    reasoning text and ``</think>`` and is trailed by a long Markdown summary.
    Scan line by line, skipping fenced code blocks, blockquotes, diff hunks and
    indented content, and match only bare, unquoted lines whose raw text equals
    an accepted termination marker. This avoids treating quoted prose, review
    JSON fields or summary text that merely mentions the phrase as a
    termination.
    """
    status, _, _ = _scan_step_limit_lines(text)
    return status == "confirmed"


def _valid_token_value(value: Any) -> int | None:
    """Return a confirmed non-negative integer token count, or ``None`` if invalid.

    ``bool`` is rejected even though it is an ``int`` subclass, as are negative
    integers, floats, strings and ``None``. A ``Mapping`` (the project's existing
    supported substructure) is summed only when every item is a non-bool
    non-negative integer; any invalid item invalidates the whole count. The caller
    distinguishes a confirmed zero from "no usage evidence" by the presence of the
    token key, so a valid zero is never mislabelled as unknown and an invalid value
    is never credited as consumption.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if value < 0:
            return None
        return value
    if isinstance(value, Mapping):
        total = 0
        for item in value.values():
            if isinstance(item, bool) or not isinstance(item, int) or item < 0:
                return None
            total += item
        return total
    return None


def _find_string(value: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if value.get(key):
            return str(value[key])
    return None


def _find_int(value: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if isinstance(value.get(key), int):
            return int(value[key])
    return None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
