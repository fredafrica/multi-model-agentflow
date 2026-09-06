"""Stable, JSON-friendly contracts shared by every AgentFlow entry point."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Mapping


def _normalized_relative(path: str) -> str:
    """Return a canonical project-relative POSIX path, rejecting escapes.

    ``.``, ``//``, and redundant components are collapsed so that ``./a``,
    ``a/./b``, and ``a//b`` all compare equal to ``a`` / ``a/b``. Absolute
    paths, parent escapes, and empty paths are rejected.
    """
    if not isinstance(path, str) or not path or "\x00" in path:
        raise ValueError(f"task path must be a non-empty relative string: {path!r}")
    pure = PurePosixPath(path)
    if pure.is_absolute() or ".." in pure.parts:
        raise ValueError(f"task path must be project-relative: {path}")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts:
        raise ValueError(f"task path must be a non-empty relative string: {path!r}")
    return "/".join(parts)


class StringEnum(str, Enum):
    """String-valued enum that serializes without provider-specific objects."""


class BusinessImportance(StringEnum):
    TRIVIAL = "B0"
    NORMAL = "B1"
    IMPORTANT = "B2"
    CRITICAL = "B3"


class OperationalSafety(StringEnum):
    LOCAL_READ_ONLY = "S0"
    REVERSIBLE_OR_PUBLIC_REMOTE = "S1"
    INTERNAL_REMOTE_OR_EXTERNAL_WRITE = "S2"
    HIGH_IMPACT_OR_IRREVERSIBLE = "S3"


class DataSensitivity(StringEnum):
    PUBLIC = "D0"
    PROJECT_INTERNAL = "D1"
    SENSITIVE_INTERNAL = "D2"
    STRICTLY_PRIVATE = "D3"


class RunMode(StringEnum):
    MANAGED = "managed"
    SUPERVISED = "supervised"
    ADAPTIVE = "adaptive"


class BudgetMode(StringEnum):
    LOCAL_FREE = "local_free"
    FIXED = "fixed"
    EFFICIENCY = "efficiency"


class ModelSelectionStrategy(StringEnum):
    LOCAL_FIRST = "local_first"
    COST_FIRST = "cost_first"
    BALANCED = "balanced"
    QUALITY_FIRST = "quality_first"


class TrustLevel(StringEnum):
    UNVERIFIED = "unverified"
    PROBATIONARY = "probationary"
    TRUSTED = "trusted"
    RESTRICTED = "restricted"


class ModelAvailabilityState(StringEnum):
    UNSUPPORTED = "unsupported"
    NOT_CONFIGURED = "not_configured"
    DISCOVERABLE = "discoverable"
    UNAVAILABLE = "unavailable"
    CALLABLE_UNVERIFIED = "callable_unverified"
    CALLABLE_VERIFIED = "callable_verified"


class Severity(StringEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class RemoteNetworkMode(StringEnum):
    DENY = "deny"
    ALLOWLIST = "allowlist"


class ReviewAcceptancePolicy(StringEnum):
    BLOCK_P0_P1 = "block_p0_p1"
    ZERO_FINDINGS = "zero_findings"


class SupervisorReasoningEffort(StringEnum):
    MEDIUM = "medium"
    HIGH = "high"


DEFAULT_IMPLEMENTATION_MAX_STEPS = 8
IMPLEMENTATION_MAX_STEPS_LIMIT = 32

DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS = 900
IMPLEMENTATION_TIMEOUT_SECONDS_MIN = 60
IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT = 14400

DEFAULT_IMPLEMENTATION_MAX_CONTINUATIONS = 0
IMPLEMENTATION_MAX_CONTINUATIONS_LIMIT = 8

DEFAULT_REMOTE_WORKER_MAX_STEPS = 32
REMOTE_WORKER_MAX_STEPS_LIMIT = 128

DEFAULT_REMOTE_WORKER_TIMEOUT_SECONDS = 900
REMOTE_WORKER_TIMEOUT_SECONDS_MIN = 60
REMOTE_WORKER_TIMEOUT_SECONDS_LIMIT = 14400

# A single authorization may never outlive one day. Longer values in a plan are
# rejected so that JSON loading and direct construction behave identically.
AUTHORIZATION_TTL_SECONDS_LIMIT = 86_400

DEFAULT_SUPERVISOR_MAX_CHECKPOINTS = 10
SUPERVISOR_MAX_CHECKPOINTS_LIMIT = 100

# Stable reason code for the single bounded sentinel checkpoint recorded when
# the per-run supervisor checkpoint limit is reached. The sentinel carries the
# dropped reason so the external Supervisor still sees a pending marker.
SUPERVISOR_CHECKPOINT_LIMIT_SENTINEL_REASON = "checkpoint_limit_reached"

DEFAULT_SUPERVISOR_MAX_CHECKPOINT_CHARS = 6000
SUPERVISOR_MAX_CHECKPOINT_CHARS_MIN = 1000
SUPERVISOR_MAX_CHECKPOINT_CHARS_LIMIT = 60000

# Stable, generic reason codes surfaced to the external Supervisor. They are
# business-neutral and only reference the actual Review/Test/state evidence.
SUPERVISOR_WAKE_EVENTS: frozenset[str] = frozenset(
    {
        "acceptance_unmet",
        "review_retries_exhausted",
        "external_evidence_unavailable",
        "unknown_call",
        "p0_p1_finding",
        "privacy_violation",
        "scope_violation",
        "authorization_violation",
        "network_violation",
        "budget_threshold",
        "cost_unavailable",
        "timeout",
        "signal_terminated",
        "session_mismatch",
        "continuation_exhausted",
        "reviewer_unavailable",
        "reviewer_protocol_error",
        "owner_decision_required",
        "run_completed",
        "run_failed",
        "run_cancelled",
        "run_paused",
        "checkpoint_limit_reached",
    }
)

# Reasons that must always wake the external Supervisor. These cannot be
# silenced by an empty (or narrow) `wake_events` configuration; the
# `wake_events` list may only add optional reasons on top of this set.
SUPERVISOR_MANDATORY_WAKE_EVENTS: frozenset[str] = frozenset(
    {
        "unknown_call",
        "p0_p1_finding",
        "privacy_violation",
        "scope_violation",
        "authorization_violation",
        "network_violation",
        "budget_threshold",
        "cost_unavailable",
        "timeout",
        "signal_terminated",
        "session_mismatch",
        "continuation_exhausted",
        "reviewer_unavailable",
        "reviewer_protocol_error",
        "owner_decision_required",
        "review_retries_exhausted",
        "run_completed",
        "run_failed",
        "run_cancelled",
        "run_paused",
        "checkpoint_limit_reached",
    }
)

# Reasons that require the escalated (high) supervisor reasoning effort rather
# than the default effort.
SUPERVISOR_ESCALATED_REASONS: frozenset[str] = frozenset(
    {
        "p0_p1_finding",
        "privacy_violation",
        "scope_violation",
        "authorization_violation",
        "network_violation",
        "unknown_call",
        "cost_unavailable",
        "budget_threshold",
        "timeout",
        "signal_terminated",
        "session_mismatch",
        "reviewer_unavailable",
        "reviewer_protocol_error",
        "review_retries_exhausted",
        "run_failed",
        "checkpoint_limit_reached",
    }
)

# Canonical mapping from the runner's internal pause reasons to the stable,
# business-neutral supervisor wake reason codes in ``SUPERVISOR_WAKE_EVENTS``.
# Only reasons that must wake the external Supervisor are listed here; a pause
# reason without a mapping (for example a user-requested ``pause_requested``)
# still records ``run_paused`` but not a specific wake reason.
SUPERVISOR_PAUSE_REASON_CODES: dict[str, str] = {
    "unknown_model_call": "unknown_call",
    "session_mismatch": "session_mismatch",
    "suspected_step_limit": "continuation_exhausted",
    "review_step_limit_reached": "continuation_exhausted",
    "implementation_step_limit_reached": "continuation_exhausted",
    "reviewer_output_invalid": "reviewer_protocol_error",
    "reviewer_unavailable": "reviewer_unavailable",
    "confirmation_required": "owner_decision_required",
    "policy_denied": "authorization_violation",
}

# Canonical mapping from an adapter's raw termination reason to the stable,
# business-neutral supervisor wake reason code. An unrecognized termination
# reason collapses to ``unknown_call`` (fail-closed, still mandatory).
SUPERVISOR_TERMINATION_REASON_CODES: dict[str, str] = {
    "timeout": "timeout",
    "signal_terminated": "signal_terminated",
}


@dataclass(frozen=True)
class RiskLevel:
    business_importance: BusinessImportance
    operational_safety: OperationalSafety


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model_id: str
    version: str
    family: str | None = None
    is_local: bool = False

    def __post_init__(self) -> None:
        validate_provider_id(self.provider)
        validate_model_id(self.model_id)
        if not self.version:
            raise ValueError("model version is required")

    @property
    def registry_key(self) -> str:
        return f"{self.provider}:{self.model_id}:{self.version}"

    @property
    def family_key(self) -> str:
        return self.family or self.model_id


@dataclass(frozen=True)
class ModelRecord:
    ref: ModelRef
    available: bool
    context_length: int | None
    tool_capable: bool
    input_cost_per_million: float | None
    output_cost_per_million: float | None
    measured_tokens_per_second: float | None
    trust_level: TrustLevel
    highest_allowed_risk: BusinessImportance
    project_success_rate: float | None = None
    first_pass_rate: float | None = None
    average_rework_count: float | None = None
    independently_reviewed_tasks: int = 0
    availability_state: ModelAvailabilityState = ModelAvailabilityState.UNAVAILABLE


@dataclass(frozen=True)
class InputArtifact:
    path: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.path or not isinstance(self.path, str):
            raise ValueError("input artifact path is required")
        if not isinstance(self.sha256, str):
            raise ValueError("input artifact sha256 must be a string")
        _normalized_relative(self.path)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", self.sha256):
            raise ValueError(f"input artifact sha256 must be a 64-char hex digest: {self.sha256}")
        object.__setattr__(self, "sha256", self.sha256.lower())


@dataclass(frozen=True)
class SupervisorPolicy:
    default_reasoning_effort: SupervisorReasoningEffort = (
        SupervisorReasoningEffort.MEDIUM
    )
    escalated_reasoning_effort: SupervisorReasoningEffort = (
        SupervisorReasoningEffort.HIGH
    )
    max_supervisor_checkpoints: int = DEFAULT_SUPERVISOR_MAX_CHECKPOINTS
    max_checkpoint_chars: int = DEFAULT_SUPERVISOR_MAX_CHECKPOINT_CHARS
    wake_events: tuple[str, ...] = ()
    continuous_llm_monitoring: bool = False
    supervisor_model_hint: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_reasoning_effort",
            SupervisorReasoningEffort(self.default_reasoning_effort),
        )
        object.__setattr__(
            self,
            "escalated_reasoning_effort",
            SupervisorReasoningEffort(self.escalated_reasoning_effort),
        )
        object.__setattr__(
            self,
            "max_supervisor_checkpoints",
            _coerce_range(
                self.max_supervisor_checkpoints,
                1,
                SUPERVISOR_MAX_CHECKPOINTS_LIMIT,
                "max_supervisor_checkpoints",
            ),
        )
        object.__setattr__(
            self,
            "max_checkpoint_chars",
            _coerce_range(
                self.max_checkpoint_chars,
                SUPERVISOR_MAX_CHECKPOINT_CHARS_MIN,
                SUPERVISOR_MAX_CHECKPOINT_CHARS_LIMIT,
                "max_checkpoint_chars",
            ),
        )
        object.__setattr__(
            self,
            "continuous_llm_monitoring",
            _require_bool(self.continuous_llm_monitoring, "continuous_llm_monitoring"),
        )
        object.__setattr__(
            self, "wake_events", _require_str_tuple(self.wake_events, "wake_events")
        )
        if self.continuous_llm_monitoring:
            raise ValueError("continuous LLM monitoring is not supported in the MVP")
        if self.supervisor_model_hint is not None:
            if not isinstance(self.supervisor_model_hint, str) or not self.supervisor_model_hint.strip():
                raise ValueError("supervisor_model_hint must be a non-empty string or null")


@dataclass(frozen=True)
class TaskContract:
    task_id: str
    objective: str
    risk_level: RiskLevel
    allowed_files: tuple[str, ...]
    forbidden_actions: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    data_sensitivity: DataSensitivity
    implementation_model: ModelRef
    review_model: ModelRef
    fallback_model: ModelRef | None
    max_remote_cost: float
    max_retry_count: int
    escalation_conditions: tuple[str, ...]
    expected_outputs: tuple[str, ...]
    depends_on: tuple[str, ...] = ()
    test_command: tuple[str, ...] = ()
    implementation_max_steps: int = DEFAULT_IMPLEMENTATION_MAX_STEPS
    implementation_timeout_seconds: int = DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS
    implementation_max_continuations: int = DEFAULT_IMPLEMENTATION_MAX_CONTINUATIONS
    allow_remote_implementation: bool = False
    remote_worker_network_mode: RemoteNetworkMode = RemoteNetworkMode.DENY
    remote_worker_allowed_hosts: tuple[str, ...] = ()
    remote_worker_max_steps: int = DEFAULT_REMOTE_WORKER_MAX_STEPS
    remote_worker_timeout_seconds: int = DEFAULT_REMOTE_WORKER_TIMEOUT_SECONDS
    input_artifacts: tuple[InputArtifact, ...] = ()
    review_acceptance_policy: ReviewAcceptancePolicy = ReviewAcceptancePolicy.BLOCK_P0_P1

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "max_remote_cost", _coerce_money(self.max_remote_cost, "max_remote_cost")
        )
        object.__setattr__(self, "max_retry_count", int(self.max_retry_count))
        object.__setattr__(
            self,
            "allow_remote_implementation",
            _require_bool(self.allow_remote_implementation, "allow_remote_implementation"),
        )
        object.__setattr__(
            self,
            "remote_worker_network_mode",
            RemoteNetworkMode(self.remote_worker_network_mode),
        )
        object.__setattr__(
            self,
            "review_acceptance_policy",
            ReviewAcceptancePolicy(self.review_acceptance_policy),
        )
        object.__setattr__(
            self, "implementation_max_steps", _coerce_step_budget(self.implementation_max_steps)
        )
        object.__setattr__(
            self,
            "implementation_timeout_seconds",
            _coerce_timeout_seconds(self.implementation_timeout_seconds),
        )
        object.__setattr__(
            self,
            "implementation_max_continuations",
            _coerce_continuations(self.implementation_max_continuations),
        )
        object.__setattr__(
            self,
            "remote_worker_max_steps",
            _coerce_step_budget(
                self.remote_worker_max_steps, upper_limit=REMOTE_WORKER_MAX_STEPS_LIMIT
            ),
        )
        object.__setattr__(
            self,
            "remote_worker_timeout_seconds",
            _coerce_timeout_seconds(self.remote_worker_timeout_seconds),
        )
        object.__setattr__(
            self, "remote_worker_allowed_hosts", _require_str_tuple(
                self.remote_worker_allowed_hosts, "remote_worker_allowed_hosts"
            )
        )
        object.__setattr__(
            self, "input_artifacts", _require_artifact_tuple(
                self.input_artifacts, "input_artifacts"
            )
        )
        if not self.task_id or not self.objective:
            raise ValueError("task_id and objective are required")
        if self.max_retry_count < 0:
            raise ValueError("retry limits cannot be negative")
        if not self.acceptance_criteria:
            raise ValueError("at least one acceptance criterion is required")
        for path in (*self.allowed_files, *self.expected_outputs):
            _normalized_relative(path)
        allowed = {_normalized_relative(path) for path in self.allowed_files}
        expected = {_normalized_relative(path) for path in self.expected_outputs}
        if not expected <= allowed:
            raise ValueError("expected outputs must be included in allowed_files")
        for host in self.remote_worker_allowed_hosts:
            validate_hostname(host)
        artifact_paths = [artifact.path for artifact in self.input_artifacts]
        normalized_artifacts = [_normalized_relative(path) for path in artifact_paths]
        if len(normalized_artifacts) != len(set(normalized_artifacts)):
            raise ValueError("input artifact paths must be unique")
        overlap = set(normalized_artifacts) & allowed
        if overlap:
            raise ValueError(
                f"input artifacts must not overlap allowed_files: {sorted(overlap)}"
            )


@dataclass(frozen=True)
class PlanContract:
    plan_id: str
    schema_version: int
    version: int
    run_mode: RunMode
    budget_mode: BudgetMode
    max_remote_cost: float
    emergency_reserve: float
    privacy_policy_version: str
    tasks: tuple[TaskContract, ...]
    max_concurrency: int = 1
    selection_strategy: ModelSelectionStrategy = ModelSelectionStrategy.LOCAL_FIRST
    allowed_model_keys: tuple[str, ...] = ()
    blocked_model_keys: tuple[str, ...] = ()
    critical_task_ids: tuple[str, ...] = ()
    allowed_provider_ids: tuple[str, ...] = ()
    authorization_ttl_seconds: int = 86_400
    supervisor_policy: SupervisorPolicy = field(default_factory=SupervisorPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", int(self.schema_version))
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(
            self, "max_remote_cost", _coerce_money(self.max_remote_cost, "max_remote_cost")
        )
        object.__setattr__(
            self, "emergency_reserve", _coerce_money(self.emergency_reserve, "emergency_reserve")
        )
        object.__setattr__(self, "max_concurrency", int(self.max_concurrency))
        object.__setattr__(
            self,
            "authorization_ttl_seconds",
            _coerce_ttl_seconds(self.authorization_ttl_seconds),
        )
        if self.schema_version < 1 or self.version < 1:
            raise ValueError("schema_version and version must be positive")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if not 1 <= len(self.tasks) <= 4:
            raise ValueError("an MVP plan must contain one to four tasks")
        task_ids = [task.task_id for task in self.tasks]
        if len(task_ids) != len(set(task_ids)):
            raise ValueError("task IDs must be unique within a plan")
        known: set[str] = set()
        for task in self.tasks:
            missing = set(task.depends_on) - known
            if missing:
                raise ValueError(
                    f"dependencies for {task.task_id} must name earlier tasks: {missing}"
                )
            known.add(task.task_id)
        listed_models = {
            model.registry_key
            for task in self.tasks
            for model in (
                task.implementation_model,
                task.review_model,
                task.fallback_model,
            )
            if model is not None
        }
        if self.allowed_model_keys and not listed_models <= set(self.allowed_model_keys):
            raise ValueError("task models must be in the plan model allowlist")
        if listed_models & set(self.blocked_model_keys):
            raise ValueError("a task model is blocked by the plan")
        if set(self.allowed_model_keys) & set(self.blocked_model_keys):
            raise ValueError("the model allowlist and blocklist must not overlap")
        listed_providers = {
            model.provider
            for task in self.tasks
            for model in (
                task.implementation_model,
                task.review_model,
                task.fallback_model,
            )
            if model is not None
        }
        for provider in self.allowed_provider_ids:
            validate_provider_id(provider)
        if self.allowed_provider_ids and not listed_providers <= set(self.allowed_provider_ids):
            raise ValueError("task providers must be in the plan provider allowlist")
        unknown_critical = set(self.critical_task_ids) - set(task_ids)
        if unknown_critical:
            raise ValueError(f"unknown critical task IDs: {unknown_critical}")
        unknown_wake_events = set(self.supervisor_policy.wake_events) - SUPERVISOR_WAKE_EVENTS
        if unknown_wake_events:
            raise ValueError(f"unknown supervisor wake events: {unknown_wake_events}")

    @property
    def provider_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    model.provider
                    for task in self.tasks
                    for model in (
                        task.implementation_model,
                        task.review_model,
                        task.fallback_model,
                    )
                    if model is not None
                }
            )
        )


@dataclass(frozen=True)
class AuthorizationSnapshot:
    authorization_id: str
    plan_id: str
    plan_version: int
    plan_hash: str
    authorized_at: datetime
    expires_at: datetime
    authorized_task_ids: tuple[str, ...]
    authorized_model_keys: tuple[str, ...]
    authorized_provider_ids: tuple[str, ...]
    allowed_files: tuple[str, ...]
    max_remote_cost: float
    run_mode: RunMode
    data_policy: Mapping[str, Any]
    privacy_policy_version: str
    max_retry_count: int
    stop_conditions: tuple[str, ...]
    escalation_conditions: tuple[str, ...]


@dataclass(frozen=True)
class InvocationRequest:
    call_id: str
    request_key: str
    run_id: str
    task_id: str
    role: str
    model: ModelRef
    prompt: str
    data_sensitivity: DataSensitivity
    read_only: bool
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InvocationResult:
    provider_request_id: str | None
    output: str
    input_tokens: int
    output_tokens: int
    first_token_latency_ms: int | None
    duration_ms: int
    remote_cost: float | None
    raw_metadata: Mapping[str, Any] = field(default_factory=dict)
    cost_unavailable: bool = False

    def __post_init__(self) -> None:
        if self.remote_cost is not None:
            if isinstance(self.remote_cost, bool) or not isinstance(
                self.remote_cost, (int, float)
            ):
                raise ValueError("reported cost must be a non-negative finite number")
            if not math.isfinite(float(self.remote_cost)) or self.remote_cost < 0:
                raise ValueError("reported cost must be a non-negative finite number")
            object.__setattr__(self, "remote_cost", float(self.remote_cost))
        if self.remote_cost is not None and self.cost_unavailable:
            raise ValueError("reported cost cannot also be unavailable")
        if self.remote_cost is None and not self.cost_unavailable:
            object.__setattr__(self, "cost_unavailable", True)


@dataclass(frozen=True)
class ReviewFinding:
    severity: Severity
    title: str
    explanation: str
    blocking: bool
    path: str | None = None
    remediation: str | None = None

    @property
    def summary(self) -> str:
        return self.title

    @property
    def evidence(self) -> str:
        return self.explanation


@dataclass(frozen=True)
class ReviewResult:
    review_id: str
    task_id: str
    reviewer: ModelRef
    findings: tuple[ReviewFinding, ...]
    approved: bool

    def __post_init__(self) -> None:
        blocking = any(item.severity in (Severity.P0, Severity.P1) for item in self.findings)
        if self.approved and blocking:
            raise ValueError("P0/P1 findings prevent approval")


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be a boolean")
    return value


def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _require_str_tuple(value: Any, label: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of strings")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{label} must contain only strings")
    return tuple(value)


def _require_artifact_tuple(
    value: Any, label: str
) -> tuple[InputArtifact, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError(f"{label} must be a list of input artifacts")
    for item in value:
        if not isinstance(item, InputArtifact):
            raise ValueError(f"{label} must contain only input artifacts")
    return tuple(value)


def _coerce_step_budget(value: Any, *, upper_limit: int = IMPLEMENTATION_MAX_STEPS_LIMIT) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("step budget must be a positive integer")
    if not 1 <= value <= upper_limit:
        raise ValueError(f"step budget must be between 1 and {upper_limit}")
    return value


def _coerce_range(value: Any, low: int, high: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if not low <= value <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return value


def _coerce_timeout_seconds(value: Any) -> int:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, int):
        raise ValueError("implementation_timeout_seconds must be an integer")
    if not IMPLEMENTATION_TIMEOUT_SECONDS_MIN <= value <= IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT:
        raise ValueError(
            "implementation_timeout_seconds must be between "
            f"{IMPLEMENTATION_TIMEOUT_SECONDS_MIN} and "
            f"{IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT}"
        )
    return value


def _coerce_continuations(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("implementation_max_continuations must be a non-negative integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("implementation_max_continuations must be a non-negative integer")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError("implementation_max_continuations must be a non-negative integer")
    if not 0 <= value <= IMPLEMENTATION_MAX_CONTINUATIONS_LIMIT:
        raise ValueError(
            "implementation_max_continuations must be between 0 and "
            f"{IMPLEMENTATION_MAX_CONTINUATIONS_LIMIT}"
        )
    return value


def _coerce_money(value: Any, label: str) -> float:
    """Validate a monetary amount: finite, non-negative, and a real number.

    Booleans and strings are rejected rather than silently coerced, so a JSON
    document and a direct constructor call fail the same way. NaN and infinity
    never pass, since they would defeat every downstream budget comparison.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a non-negative finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return number


def _coerce_ttl_seconds(value: Any) -> int:
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, int):
        raise ValueError("authorization_ttl_seconds must be an integer")
    if not 1 <= value <= AUTHORIZATION_TTL_SECONDS_LIMIT:
        raise ValueError(
            "authorization_ttl_seconds must be between 1 and "
            f"{AUTHORIZATION_TTL_SECONDS_LIMIT}"
        )
    return value


_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,255}$")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_HOST_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def validate_provider_id(value: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise ValueError(f"unsupported provider identifier: {value!r}")
    return value


def validate_model_id(value: str) -> str:
    if not _MODEL_ID.fullmatch(value):
        raise ValueError(f"unsupported model identifier: {value!r}")
    return value


def validate_hostname(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"unsupported hostname: {value!r}")
    if not value or not _HOSTNAME.fullmatch(value):
        raise ValueError(f"unsupported hostname: {value!r}")
    for label in value.split("."):
        if not _HOST_LABEL.fullmatch(label):
            raise ValueError(f"unsupported hostname: {value!r}")
    return value
