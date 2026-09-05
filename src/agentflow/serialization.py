"""Canonical serialization and parsing for plan contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping

from .contracts import (
    AuthorizationSnapshot,
    BudgetMode,
    BusinessImportance,
    DataSensitivity,
    ModelSelectionStrategy,
    ModelRef,
    OperationalSafety,
    PlanContract,
    RiskLevel,
    RunMode,
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


def model_from_mapping(data: Mapping[str, Any]) -> ModelRef:
    return ModelRef(
        provider=str(data["provider"]),
        model_id=str(data["model_id"]),
        version=str(data["version"]),
        family=str(data["family"]) if data.get("family") else None,
        is_local=bool(data["is_local"]),
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
        max_remote_cost=float(data["max_remote_cost"]),
        max_retry_count=int(data["max_retry_count"]),
        escalation_conditions=tuple(str(item) for item in data["escalation_conditions"]),
        expected_outputs=tuple(str(item) for item in data["expected_outputs"]),
        depends_on=tuple(str(item) for item in data.get("depends_on", ())),
        test_command=tuple(str(item) for item in data.get("test_command", ())),
    )


def plan_from_mapping(data: Mapping[str, Any]) -> PlanContract:
    return PlanContract(
        plan_id=str(data["plan_id"]),
        schema_version=int(data["schema_version"]),
        version=int(data["version"]),
        run_mode=RunMode(data["run_mode"]),
        budget_mode=BudgetMode(data["budget_mode"]),
        max_remote_cost=float(data["max_remote_cost"]),
        emergency_reserve=float(data["emergency_reserve"]),
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
        authorization_ttl_seconds=int(data.get("authorization_ttl_seconds", 86_400)),
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
