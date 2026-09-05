"""Stable, JSON-friendly contracts shared by every AgentFlow entry point."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import PurePath
from typing import Any, Mapping


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


DEFAULT_IMPLEMENTATION_MAX_STEPS = 8
IMPLEMENTATION_MAX_STEPS_LIMIT = 32

DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS = 900
IMPLEMENTATION_TIMEOUT_SECONDS_MIN = 60
IMPLEMENTATION_TIMEOUT_SECONDS_LIMIT = 14400

DEFAULT_IMPLEMENTATION_MAX_CONTINUATIONS = 0
IMPLEMENTATION_MAX_CONTINUATIONS_LIMIT = 8


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "max_remote_cost", float(self.max_remote_cost))
        object.__setattr__(self, "max_retry_count", int(self.max_retry_count))
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
        if not self.task_id or not self.objective:
            raise ValueError("task_id and objective are required")
        if self.max_remote_cost < 0 or self.max_retry_count < 0:
            raise ValueError("cost and retry limits cannot be negative")
        if not self.acceptance_criteria:
            raise ValueError("at least one acceptance criterion is required")
        for path in (*self.allowed_files, *self.expected_outputs):
            pure = PurePath(path)
            if pure.is_absolute() or ".." in pure.parts:
                raise ValueError(f"task paths must be project-relative: {path}")
        if not set(self.expected_outputs) <= set(self.allowed_files):
            raise ValueError("expected outputs must be included in allowed_files")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", int(self.schema_version))
        object.__setattr__(self, "version", int(self.version))
        object.__setattr__(self, "max_remote_cost", float(self.max_remote_cost))
        object.__setattr__(self, "emergency_reserve", float(self.emergency_reserve))
        object.__setattr__(self, "max_concurrency", int(self.max_concurrency))
        object.__setattr__(self, "authorization_ttl_seconds", int(self.authorization_ttl_seconds))
        if self.schema_version < 1 or self.version < 1:
            raise ValueError("schema_version and version must be positive")
        if self.max_remote_cost < 0 or self.emergency_reserve < 0:
            raise ValueError("plan budgets cannot be negative")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if self.authorization_ttl_seconds < 1:
            raise ValueError("authorization_ttl_seconds must be positive")
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
        if self.remote_cost is not None and self.remote_cost < 0:
            raise ValueError("reported cost cannot be negative")
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


def _coerce_step_budget(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("implementation_max_steps must be a positive integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("implementation_max_steps must be a positive integer")
        value = int(value)
    if not isinstance(value, int):
        raise ValueError("implementation_max_steps must be a positive integer")
    if not 1 <= value <= IMPLEMENTATION_MAX_STEPS_LIMIT:
        raise ValueError(
            "implementation_max_steps must be between 1 and "
            f"{IMPLEMENTATION_MAX_STEPS_LIMIT}"
        )
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


_PROVIDER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,255}$")


def validate_provider_id(value: str) -> str:
    if not _PROVIDER_ID.fullmatch(value):
        raise ValueError(f"unsupported provider identifier: {value!r}")
    return value


def validate_model_id(value: str) -> str:
    if not _MODEL_ID.fullmatch(value):
        raise ValueError(f"unsupported model identifier: {value!r}")
    return value
