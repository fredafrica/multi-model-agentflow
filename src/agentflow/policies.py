"""Deterministic authorization, privacy, budget, review, and file gates."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePath
from typing import Iterable

from .contracts import (
    AuthorizationSnapshot,
    BudgetMode,
    BusinessImportance,
    DataSensitivity,
    InvocationRequest,
    OperationalSafety,
    PlanContract,
    RunMode,
    TaskContract,
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reasons: tuple[str, ...] = ()
    requires_confirmation: bool = False


_SECRET_PATTERNS = {
    "credential_field": re.compile(
        r"(?i)(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*\S+"
    ),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    "personal_path": re.compile(r"(?:/Users/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)"),
    "account_field": re.compile(
        r"(?i)(account[_-]?id|account[_-]?number|routing[_-]?number)\s*[:=]\s*\S+"
    ),
}


def scan_sensitive_text(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in _SECRET_PATTERNS.items() if pattern.search(text))


def remote_data_decision(
    sensitivity: DataSensitivity,
    *,
    authorization: AuthorizationSnapshot,
    redaction_passed: bool,
    content: str,
) -> PolicyDecision:
    findings = scan_sensitive_text(content)
    if findings:
        return PolicyDecision(False, tuple(f"detected:{item}" for item in findings))
    if sensitivity is DataSensitivity.STRICTLY_PRIVATE:
        return PolicyDecision(False, ("D3 data cannot be sent remotely",))
    if sensitivity is DataSensitivity.PROJECT_INTERNAL:
        if not authorization.data_policy.get("allow_d1_remote", False):
            return PolicyDecision(False, ("D1 is not authorized for this project plan",))
    if sensitivity is DataSensitivity.SENSITIVE_INTERNAL:
        if not authorization.data_policy.get("allow_d2_remote", False):
            return PolicyDecision(False, ("D2 is not authorized for this plan",))
        if not redaction_passed:
            return PolicyDecision(False, ("D2 redaction did not pass",))
    return PolicyDecision(True)


def invocation_decision(
    request: InvocationRequest,
    *,
    task: TaskContract,
    plan: PlanContract,
    authorization: AuthorizationSnapshot,
    estimated_remote_cost: float,
    remote_cost_spent: float,
    redaction_passed: bool = False,
) -> PolicyDecision:
    reasons: list[str] = []
    if request.task_id not in authorization.authorized_task_ids:
        reasons.append("task is outside the authorization")
    if request.model.registry_key not in authorization.authorized_model_keys:
        reasons.append("model is outside the authorization")
    if request.model.registry_key in plan.blocked_model_keys:
        reasons.append("model is blocked by the plan")
    if plan.allowed_model_keys and request.model.registry_key not in plan.allowed_model_keys:
        reasons.append("model is outside the plan allowlist")
    if estimated_remote_cost < 0:
        reasons.append("estimated cost cannot be negative")
    if not request.model.is_local:
        if plan.budget_mode is BudgetMode.LOCAL_FREE:
            reasons.append("local-free mode prohibits remote models")
        usable_budget = max(0.0, authorization.max_remote_cost - plan.emergency_reserve)
        if remote_cost_spent + estimated_remote_cost > usable_budget:
            reasons.append("remote cost would exceed the usable budget")
        privacy = remote_data_decision(
            request.data_sensitivity,
            authorization=authorization,
            redaction_passed=redaction_passed,
            content=request.prompt,
        )
        reasons.extend(privacy.reasons)
    confirmation = plan.run_mode is RunMode.SUPERVISED
    if plan.run_mode is RunMode.ADAPTIVE:
        confirmation = (
            task.task_id in plan.critical_task_ids
            or task.risk_level.business_importance
            in (BusinessImportance.IMPORTANT, BusinessImportance.CRITICAL)
            or task.risk_level.operational_safety
            in (
                OperationalSafety.INTERNAL_REMOTE_OR_EXTERNAL_WRITE,
                OperationalSafety.HIGH_IMPACT_OR_IRREVERSIBLE,
            )
        )
    return PolicyDecision(not reasons, tuple(reasons), confirmation)


def review_independence_decision(task: TaskContract) -> PolicyDecision:
    implementation = task.implementation_model
    reviewer = task.review_model
    if implementation.registry_key == reviewer.registry_key:
        return PolicyDecision(False, ("implementation and review cannot use the same model version",))
    if task.risk_level.business_importance in (
        BusinessImportance.IMPORTANT,
        BusinessImportance.CRITICAL,
    ) and implementation.family_key == reviewer.family_key:
        return PolicyDecision(False, ("important tasks require a different model family",))
    return PolicyDecision(True)


def changed_files_decision(
    allowed_files: Iterable[str], changed_files: Iterable[str]
) -> PolicyDecision:
    allowed = {PurePath(item).as_posix() for item in allowed_files}
    changed = {PurePath(item).as_posix() for item in changed_files}
    outside = sorted(changed - allowed)
    if outside:
        return PolicyDecision(False, tuple(f"file outside task scope:{item}" for item in outside))
    return PolicyDecision(True)
