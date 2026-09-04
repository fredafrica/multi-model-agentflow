"""OpenCode adapter restricted to the local LM Studio provider."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .adapters import InvocationOutcomeUnknown
from .contracts import (
    BusinessImportance,
    InvocationRequest,
    InvocationResult,
    ModelRecord,
    ModelRef,
    TrustLevel,
)


class OpenCodeAdapter:
    adapter_id = "opencode-lmstudio"

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
                )
            )
        return records

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        if request.model.provider != "lmstudio":
            raise ValueError("OpenCodeAdapter only permits the local lmstudio provider")
        worktree = Path(str(request.metadata["worktree"])).resolve()
        if not worktree.is_dir():
            raise ValueError("invocation worktree does not exist")
        prompt = self._bounded_prompt(request)
        environment = os.environ.copy()
        environment["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            self._permission_config(read_only=request.read_only), separators=(",", ":")
        )
        command = (
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
            "--dir",
            str(worktree),
            prompt,
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
        process_reference = f"local-process-group:{process.pid}"
        callback = request.metadata.get("on_provider_request_id")
        if callable(callback):
            callback(process_reference)
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self.cancel(process_reference)
            process.communicate()
            raise InvocationOutcomeUnknown(
                "OpenCode timed out; inspect the local worktree before retrying",
                process_reference,
            ) from error
        duration_ms = int((time.monotonic() - started) * 1000)
        if process.returncode:
            raise RuntimeError(stderr.strip() or "OpenCode invocation failed")
        return parse_opencode_json(stdout, duration_ms=duration_ms)

    def query(self, provider_request_id: str) -> InvocationResult | None:
        return None

    def cancel(self, provider_request_id: str) -> bool:
        prefix = "local-process-group:"
        if not provider_request_id.startswith(prefix):
            return False
        try:
            process_group = int(provider_request_id.removeprefix(prefix))
            os.killpg(process_group, signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            return False
        return True

    @staticmethod
    def _permission_config(*, read_only: bool) -> dict[str, Any]:
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
            "bash": "deny",
            "external_directory": "deny",
            "webfetch": "deny",
            "websearch": "deny",
            "task": "deny",
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
                    "steps": 8,
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


def parse_opencode_json(text: str, *, duration_ms: int) -> InvocationResult:
    output_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    provider_request_id: str | None = None
    first_token_latency_ms: int | None = None
    remote_cost = 0.0
    event_types: list[str] = []

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        event_types.append(str(event.get("type", "unknown")))
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
        input_tokens = max(input_tokens, _token_total(tokens.get("input")))
        output_tokens = max(output_tokens, _token_total(tokens.get("output")))
        cost = part.get("cost", event.get("cost"))
        if isinstance(cost, (int, float)):
            remote_cost = max(remote_cost, float(cost))

    output = "".join(output_parts).strip()
    if not output:
        raise RuntimeError("OpenCode returned no text event")
    return InvocationResult(
        provider_request_id=provider_request_id,
        output=output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        first_token_latency_ms=first_token_latency_ms,
        duration_ms=duration_ms,
        remote_cost=0.0,
        raw_metadata={"event_types": event_types, "reported_cost": remote_cost},
    )


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
