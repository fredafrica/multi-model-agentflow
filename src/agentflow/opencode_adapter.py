"""OpenCode adapters for local execution and packet-only remote review."""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .adapters import (
    InvocationIncompleteError,
    InvocationOutcomeUnknown,
    ModelUnavailableError,
    ProviderNotConfiguredError,
    UnsupportedProviderError,
)
from .contracts import (
    BusinessImportance,
    DEFAULT_IMPLEMENTATION_MAX_STEPS,
    DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    IMPLEMENTATION_MAX_STEPS_LIMIT,
    IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT,
    IMPLEMENTATION_TIMEOUT_SECONDS_MIN,
    InvocationRequest,
    InvocationResult,
    ModelAvailabilityState,
    ModelRecord,
    ModelRef,
    TrustLevel,
    validate_model_id,
    validate_provider_id,
)


# One normal reviewer response turn plus OpenCode's bounded finalization turn.
REMOTE_REVIEWER_MAX_STEPS = 2


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
                    context_length=_optional_int(item.get("contextLength")),
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
        worktree = Path(str(request.metadata["worktree"])).resolve()
        if not worktree.is_dir():
            raise ValueError("invocation worktree does not exist")
        steps = _implementation_steps(request.metadata)
        timeout_seconds = _implementation_timeout(request.metadata)
        prompt = self._bounded_prompt(request)
        environment = os.environ.copy()
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            self._permission_config(read_only=request.read_only, steps=steps),
            separators=(",", ":"),
        )
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
            partial_stdout = _collect_timeout_output(error, process)
            duration_ms = int((time.monotonic() - started) * 1000)
            result = parse_opencode_partial_usage(
                partial_stdout,
                duration_ms=duration_ms,
                timeout_seconds=timeout_seconds,
            )
            raise InvocationOutcomeUnknown(
                "OpenCode timed out; inspect the local worktree before retrying",
                process_reference,
                result=result,
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if process.returncode is not None and process.returncode < 0:
            raise InvocationOutcomeUnknown(
                "OpenCode was terminated by a signal; inspect the local worktree "
                "before retrying",
                process_reference,
            )
        if process.returncode:
            try:
                parse_opencode_json(stdout, duration_ms=duration_ms, is_local=True)
            except InvocationIncompleteError as error:
                if session_id is not None:
                    _verify_session_reuse(session_id, error.result)
                raise
            except RuntimeError:
                pass
            raise RuntimeError(stderr.strip() or "OpenCode invocation failed")
        result = parse_opencode_json(stdout, duration_ms=duration_ms, is_local=True)
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
            "Read-only review. Do not modify any file."
            if request.read_only
            else f"You may edit only these project-relative files:\n{allowed}"
        )
        return (
            "You are running inside an isolated Git worktree. "
            "Do not access external directories or networks.\n"
            f"{mode}\n\n{request.prompt}"
        )


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
        try:
            result = subprocess.run(
                (self.opencode_command, "models", self.provider, "--pure"),
                text=True,
                capture_output=True,
                check=False,
                timeout=self.discovery_timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as error:
            raise UnsupportedProviderError(
                "unsupported provider: OpenCode model discovery is unavailable"
            ) from error
        if result.returncode:
            raise ProviderNotConfiguredError(
                f"provider not configured: {self.provider}"
            )
        prefix = f"{self.provider}/"
        model_ids = tuple(
            line.strip().removeprefix(prefix)
            for line in result.stdout.splitlines()
            if line.strip().startswith(prefix)
        )
        if not model_ids:
            raise ProviderNotConfiguredError(
                f"provider not configured: {self.provider}"
            )
        for model_id in model_ids:
            validate_model_id(model_id)
        return model_ids

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
        self.require_model(request.model)
        environment = os.environ.copy()
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            self._permission_config(), separators=(",", ":")
        )
        with tempfile.TemporaryDirectory(prefix="agentflow-review-") as directory:
            review_root = Path(directory)
            review_root.chmod(0o500)
            command = (
                self.opencode_command,
                "run",
                "--format",
                "json",
                "--pure",
                "--auto",
                "--agent",
                "agentflow-remote-reviewer",
                "--model",
                f"{self.provider}/{request.model.model_id}",
                "--dir",
                str(review_root),
                request.prompt,
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
                stdout, stderr = process.communicate(timeout=self.timeout_seconds)
            except (subprocess.TimeoutExpired, KeyboardInterrupt, OSError) as error:
                OpenCodeAdapter().cancel(process_reference)
                process.communicate()
                raise InvocationOutcomeUnknown(
                    "invocation outcome unknown: remote OpenCode review was interrupted",
                    process_reference,
                ) from error
            duration_ms = int((time.monotonic() - started) * 1000)
            if process.returncode is not None and process.returncode < 0:
                raise InvocationOutcomeUnknown(
                    "invocation outcome unknown: remote OpenCode review was terminated",
                    process_reference,
                )
            if process.returncode:
                try:
                    parse_opencode_json(stdout, duration_ms=duration_ms, is_local=False)
                except InvocationIncompleteError:
                    raise
                except RuntimeError:
                    pass
                raise ModelUnavailableError(
                    "model unavailable: "
                    + (stderr.strip() or f"{self.provider}/{request.model.model_id}")
                )
            try:
                return parse_opencode_json(
                    stdout, duration_ms=duration_ms, is_local=False
                )
            except InvocationIncompleteError:
                raise
            except RuntimeError as error:
                raise ModelUnavailableError(
                    "model unavailable: remote reviewer returned no usable result"
                ) from error

    def query(self, provider_request_id: str) -> InvocationResult | None:
        return None

    def cancel(self, provider_request_id: str) -> bool:
        return OpenCodeAdapter().cancel(provider_request_id)

    def _permission_config(self) -> dict[str, Any]:
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
                    "steps": REMOTE_REVIEWER_MAX_STEPS,
                    "permission": permission,
                }
            },
        }


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


def _collect_timeout_output(
    error: subprocess.TimeoutExpired, process: subprocess.Popen
) -> bytes:
    first = _as_output_bytes(error.output)
    try:
        second_stdout, _ = process.communicate()
    except Exception:
        second_stdout = b""
    return _merge_overlapping_output_bytes(first, _as_output_bytes(second_stdout))


def parse_opencode_partial_usage(
    data: bytes, *, duration_ms: int, timeout_seconds: int
) -> InvocationResult:
    """Extract conservative usage evidence from partial OpenCode JSON events.

    The output text is intentionally empty: a timed-out call has no confirmed
    result. Each completed step reports its own token usage, so every real
    token-bearing event in the merged stream is summed. Overlap between the two
    reads is removed at the raw byte-stream level before decoding; events are
    never de-duplicated by their content.
    """
    text = data.decode("utf-8", errors="replace")
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    saw_reasoning = False
    provider_request_id: str | None = None
    event_count = 0
    completed_step_count = 0
    saw_usage = False

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_count += 1
        provider_request_id = provider_request_id or _find_string(
            event, ("sessionID", "sessionId", "session_id")
        )
        if str(event.get("type", "")) == "step_finish":
            completed_step_count += 1
        part = event.get("part") if isinstance(event.get("part"), Mapping) else event
        tokens = part.get("tokens") if isinstance(part.get("tokens"), Mapping) else {}
        in_tokens = _token_total(tokens.get("input"))
        out_tokens = _token_total(tokens.get("output"))
        if in_tokens or out_tokens:
            saw_usage = True
        input_tokens += in_tokens
        output_tokens += out_tokens
        reasoning = _token_total(tokens.get("reasoning"))
        if reasoning:
            saw_reasoning = True
            reasoning_tokens += reasoning

    metadata = {
        "termination_reason": "timeout",
        "timeout_seconds": timeout_seconds,
        "token_source": "opencode_json_events" if saw_usage else "unavailable",
        "usage_unavailable": not saw_usage,
        "event_count": event_count,
        "completed_step_count": completed_step_count,
        "session_id": provider_request_id,
        "reasoning_tokens": reasoning_tokens if saw_reasoning else None,
        "partial_stdout_bytes": len(data),
        "partial_stdout_sha256": hashlib.sha256(data).hexdigest(),
    }
    return InvocationResult(
        provider_request_id=provider_request_id,
        output="",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        first_token_latency_ms=None,
        duration_ms=duration_ms,
        remote_cost=0.0,
        raw_metadata=metadata,
        cost_unavailable=False,
    )


def parse_opencode_json(
    text: str, *, duration_ms: int, is_local: bool = False
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
    event_types: list[str] = []
    structured_step_limit = False

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_types.append(str(event.get("type", "unknown")))
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
        tokens = part.get("tokens") if isinstance(part.get("tokens"), Mapping) else {}
        input_tokens += _token_total(tokens.get("input"))
        output_tokens += _token_total(tokens.get("output"))
        reasoning = _token_total(tokens.get("reasoning"))
        if reasoning:
            saw_reasoning = True
            reasoning_tokens += reasoning
        cost = part.get("cost", event.get("cost"))
        if isinstance(cost, (int, float)):
            reported_cost += float(cost)
            cost_reported = True

    output = "".join(output_parts).strip()
    if not output and not structured_step_limit:
        raise RuntimeError("OpenCode returned no text event")
    final_text_step_limit = bool(output_parts) and _text_reports_step_limit(output)
    termination_source = (
        "structured_event"
        if structured_step_limit
        else "final_text"
        if final_text_step_limit
        else None
    )
    metadata = {
        "event_types": event_types,
        "reported_cost": reported_cost if cost_reported else None,
        "cost_unavailable": not is_local and not cost_reported,
        "reasoning_tokens": reasoning_tokens if saw_reasoning else None,
    }
    if termination_source is not None:
        metadata.update(
            {
                "failure_kind": "step_limit_reached",
                "termination_source": termination_source,
            }
        )
    result = InvocationResult(
        provider_request_id=provider_request_id,
        output=output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        first_token_latency_ms=first_token_latency_ms,
        duration_ms=duration_ms,
        remote_cost=0.0 if is_local else (reported_cost if cost_reported else None),
        raw_metadata=metadata,
        cost_unavailable=not is_local and not cost_reported,
    )
    if termination_source is not None:
        raise InvocationIncompleteError(
            "OpenCode reached its maximum step limit before completing the invocation",
            result,
            failure_kind="step_limit_reached",
        )
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


_STEP_LIMIT_LINE_RE = re.compile(
    r"(?:"
    r"(?:the )?max(?:imum)?(?: number of)? steps(?: for this agent)? "
    r"(?:have been|has been|were|was) reached"
    r"|critical\s*[-–—]\s*max(?:imum)? steps reached"
    r")\.?",
    re.IGNORECASE,
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
    in_fence = False
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
        if _line_is_step_limit_marker(stripped):
            return True
    return False


def _token_total(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, Mapping):
        return sum(int(item) for item in value.values() if isinstance(item, int))
    return 0


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
