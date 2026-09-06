"""Canonical serialization and parsing for plan contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .contracts import (
    DEFAULT_IMPLEMENTATION_MAX_CONTINUATIONS,
    DEFAULT_IMPLEMENTATION_MAX_STEPS,
    DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS,
    DEFAULT_REMOTE_WORKER_MAX_STEPS,
    DEFAULT_REMOTE_WORKER_TIMEOUT_SECONDS,
    DEFAULT_SUPERVISOR_MAX_CHECKPOINT_CHARS,
    DEFAULT_SUPERVISOR_MAX_CHECKPOINTS,
    AuthorizationSnapshot,
    BudgetMode,
    BusinessImportance,
    DataSensitivity,
    InputArtifact,
    ModelRef,
    ModelSelectionStrategy,
    OperationalSafety,
    PlanContract,
    RemoteNetworkMode,
    ReviewAcceptancePolicy,
    RiskLevel,
    RunMode,
    SupervisorPolicy,
    SupervisorReasoningEffort,
    TaskContract,
)


def to_primitive(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_primitive(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        to_primitive(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def plan_hash(plan: PlanContract) -> str:
    return hashlib.sha256(canonical_json(plan).encode("utf-8")).hexdigest()


def digest_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_bool(data: Mapping[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _require_str_list(data: Mapping[str, Any], key: str) -> tuple[str, ...]:
    if key not in data:
        return ()
    value = data[key]
    if value is None:
        raise ValueError(f"{key} must be a list of strings, not null")
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{key} must contain only strings")
        result.append(item)
    return tuple(result)


def _require_input_artifacts(
    data: Mapping[str, Any], key: str
) -> tuple[InputArtifact, ...]:
    if key not in data:
        return ()
    value = data[key]
    if value is None:
        raise ValueError(f"{key} must be a list of objects, not null")
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{key} must be a list of objects")
    result: list[InputArtifact] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError(f"{key} must contain only objects")
        if "path" not in item or "sha256" not in item:
            raise ValueError(f"{key} entries must include path and sha256")
        result.append(InputArtifact(path=item["path"], sha256=item["sha256"]))
    return tuple(result)


def model_from_mapping(data: Mapping[str, Any]) -> ModelRef:
    return ModelRef(
        provider=str(data["provider"]),
        model_id=str(data["model_id"]),
        version=str(data["version"]),
        family=str(data["family"]) if data.get("family") else None,
        is_local=_require_bool(data, "is_local"),
    )


def task_from_mapping(data: Mapping[str, Any]) -> TaskContract:
    risk = data["risk_level"]
    fallback = data.get("fallback_model")
    return TaskContract(
        task_id=str(data["task_id"]),
        objective=str(data["objective"]),
        risk_level=RiskLevel(
            BusinessImportance(risk["business_importance"]),
            OperationalSafety(risk["operational_safety"]),
        ),
        allowed_files=tuple(str(item) for item in data["allowed_files"]),
        forbidden_actions=tuple(str(item) for item in data["forbidden_actions"]),
        acceptance_criteria=tuple(str(item) for item in data["acceptance_criteria"]),
        data_sensitivity=DataSensitivity(data["data_sensitivity"]),
        implementation_model=model_from_mapping(data["implementation_model"]),
        review_model=model_from_mapping(data["review_model"]),
        fallback_model=model_from_mapping(fallback) if fallback else None,
        max_remote_cost=data["max_remote_cost"],
        max_retry_count=data["max_retry_count"],
        escalation_conditions=tuple(str(item) for item in data["escalation_conditions"]),
        expected_outputs=tuple(str(item) for item in data["expected_outputs"]),
        depends_on=tuple(str(item) for item in data.get("depends_on", ())),
        test_command=tuple(str(item) for item in data.get("test_command", ())),
        implementation_max_steps=data.get(
            "implementation_max_steps", DEFAULT_IMPLEMENTATION_MAX_STEPS
        ),
        implementation_timeout_seconds=data.get(
            "implementation_timeout_seconds", DEFAULT_IMPLEMENTATION_TIMEOUT_SECONDS
        ),
        implementation_max_continuations=data.get(
            "implementation_max_continuations", DEFAULT_IMPLEMENTATION_MAX_CONTINUATIONS
        ),
        allow_remote_implementation=data.get("allow_remote_implementation", False),
        remote_worker_network_mode=RemoteNetworkMode(
            data.get("remote_worker_network_mode", RemoteNetworkMode.DENY.value)
        ),
        remote_worker_allowed_hosts=_require_str_list(
            data, "remote_worker_allowed_hosts"
        ),
        remote_worker_max_steps=data.get(
            "remote_worker_max_steps", DEFAULT_REMOTE_WORKER_MAX_STEPS
        ),
        remote_worker_timeout_seconds=data.get(
            "remote_worker_timeout_seconds", DEFAULT_REMOTE_WORKER_TIMEOUT_SECONDS
        ),
        input_artifacts=_require_input_artifacts(data, "input_artifacts"),
        review_acceptance_policy=ReviewAcceptancePolicy(
            data.get("review_acceptance_policy", ReviewAcceptancePolicy.BLOCK_P0_P1.value)
        ),
    )


def supervisor_policy_from_mapping(data: Mapping[str, Any]) -> SupervisorPolicy:
    return SupervisorPolicy(
        default_reasoning_effort=SupervisorReasoningEffort(
            data.get(
                "default_reasoning_effort",
                SupervisorReasoningEffort.MEDIUM.value,
            )
        ),
        escalated_reasoning_effort=SupervisorReasoningEffort(
            data.get(
                "escalated_reasoning_effort",
                SupervisorReasoningEffort.HIGH.value,
            )
        ),
        max_supervisor_checkpoints=data.get(
            "max_supervisor_checkpoints", DEFAULT_SUPERVISOR_MAX_CHECKPOINTS
        ),
        max_checkpoint_chars=data.get(
            "max_checkpoint_chars", DEFAULT_SUPERVISOR_MAX_CHECKPOINT_CHARS
        ),
        wake_events=_require_str_list(data, "wake_events"),
        continuous_llm_monitoring=data.get("continuous_llm_monitoring", False),
        supervisor_model_hint=(
            data.get("supervisor_model_hint")
            if data.get("supervisor_model_hint") is not None
            else None
        ),
    )


def plan_from_mapping(data: Mapping[str, Any]) -> PlanContract:
    return PlanContract(
        plan_id=str(data["plan_id"]),
        schema_version=int(data["schema_version"]),
        version=int(data["version"]),
        run_mode=RunMode(data["run_mode"]),
        budget_mode=BudgetMode(data["budget_mode"]),
        max_remote_cost=data["max_remote_cost"],
        emergency_reserve=data["emergency_reserve"],
        privacy_policy_version=str(data["privacy_policy_version"]),
        tasks=tuple(task_from_mapping(item) for item in data["tasks"]),
        max_concurrency=int(data.get("max_concurrency", 1)),
        selection_strategy=ModelSelectionStrategy(
            data.get("selection_strategy", ModelSelectionStrategy.LOCAL_FIRST.value)
        ),
        allowed_model_keys=tuple(str(item) for item in data.get("allowed_model_keys", ())),
        blocked_model_keys=tuple(str(item) for item in data.get("blocked_model_keys", ())),
        critical_task_ids=tuple(str(item) for item in data.get("critical_task_ids", ())),
        allowed_provider_ids=tuple(
            str(item) for item in data.get("allowed_provider_ids", ())
        ),
        authorization_ttl_seconds=data.get("authorization_ttl_seconds", 86_400),
        supervisor_policy=supervisor_policy_from_mapping(data.get("supervisor_policy", {})),
    )


def load_plan_json(text: str) -> PlanContract:
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("plan JSON must contain an object")
    return plan_from_mapping(data)


def authorization_from_mapping(data: Mapping[str, Any]) -> AuthorizationSnapshot:
    return AuthorizationSnapshot(
        authorization_id=str(data["authorization_id"]),
        plan_id=str(data["plan_id"]),
        plan_version=int(data["plan_version"]),
        plan_hash=str(data["plan_hash"]),
        authorized_at=datetime.fromisoformat(data["authorized_at"]),
        expires_at=datetime.fromisoformat(data["expires_at"]),
        authorized_task_ids=tuple(str(item) for item in data["authorized_task_ids"]),
        authorized_model_keys=tuple(str(item) for item in data["authorized_model_keys"]),
        authorized_provider_ids=tuple(
            str(item)
            for item in data.get(
                "authorized_provider_ids",
                sorted({str(key).split(":", 1)[0] for key in data["authorized_model_keys"]}),
            )
        ),
        allowed_files=tuple(str(item) for item in data["allowed_files"]),
        max_remote_cost=float(data["max_remote_cost"]),
        run_mode=RunMode(data["run_mode"]),
        data_policy=dict(data["data_policy"]),
        privacy_policy_version=str(data.get("privacy_policy_version", "unknown")),
        max_retry_count=int(data["max_retry_count"]),
        stop_conditions=tuple(str(item) for item in data["stop_conditions"]),
        escalation_conditions=tuple(str(item) for item in data["escalation_conditions"]),
    )
