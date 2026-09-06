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
    RemoteNetworkMode,
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
        r'''(?ix)
        ["']?
        (?:api[_-]?key|access[_-]?token|auth[_-]?token|token|password|secret|credential)
        ["']?\s*[:=]\s*["']?[^\s,"']+
        '''
    ),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{12,}"),
    "personal_path": re.compile(
        r"(?:/Users/[^/\s]+|/home/[^/\s]+|[A-Za-z]:\\Users\\[^\\\s]+)"
    ),
    "account_field": re.compile(
        r'''(?ix)
        ["']?
        (?:account|account[_-]?id|account[_-]?number|routing[_-]?number|username)
        ["']?\s*[:=]\s*["']?[^\s,"']+
        '''
    ),
}


def scan_sensitive_text(text: str) -> tuple[str, ...]:
    return tuple(name for name, pattern in _SECRET_PATTERNS.items() if pattern.search(text))


def classify_policy_denial(reasons: Iterable[str]) -> str:
    """Map invocation-denial reasons to a stable supervisor reason code.

    The most specific violation wins; the default is ``authorization_violation``.
    """
    for reason in reasons:
        if reason.startswith("privacy denied:") or reason.startswith("detected:"):
            return "privacy_violation"
    for reason in reasons:
        if reason.startswith("budget denied:") or "cost cannot be negative" in reason:
            return "budget_threshold"
    for reason in reasons:
        if reason.startswith("network denied:"):
            return "network_violation"
    return "authorization_violation"


def network_decision(
    mode: RemoteNetworkMode,
    allowed_hosts: Iterable[str],
    target_host: str,
) -> PolicyDecision:
    """Decide whether a remote worker may contact a host, fail-closed.

    This is a pure, deterministic gate that can be enforced directly when the
    adapter layer can express host allowlists. The OpenCode permission layer
    cannot express a per-host allowlist for ``webfetch``/``websearch``, so the
    worker adapter always denies those tools and the invocation gate rejects
    ``ALLOWLIST`` mode; this function remains the reference for the intended
    semantics and is exercised independently by tests.
    """
    if mode is RemoteNetworkMode.DENY:
        return PolicyDecision(False, ("network denied by the task's remote worker policy",))
    if not target_host:
        return PolicyDecision(False, ("a network target host is required for allowlist mode",))
    normalized = target_host.lower().rstrip(".")
    allowed = {host.lower().rstrip(".") for host in allowed_hosts}
    if normalized in allowed or any(normalized.endswith("." + host) for host in allowed):
        return PolicyDecision(True)
    return PolicyDecision(False, (f"host is outside the remote worker allowlist: {target_host}",))


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


def remote_material_decision(
    sensitivity: DataSensitivity,
    *,
    authorization: AuthorizationSnapshot,
    redaction_passed: bool,
    material: Iterable[str],
) -> PolicyDecision:
    """Scan every piece of content a remote model can read, fail-closed.

    Unlike :func:`remote_data_decision` this takes the complete set of strings
    a remote worker/reviewer can observe (the prompt, generated briefing, the
    actual snapshot contents of input artifacts, and the contents of pre-existing
    allowed files). The sensitive-field scan runs over all of it so a secret that
    lives in an input file or an existing allowed output is blocked before any
    remote request is sent. No matched secret text is returned; only the pattern
    category name is surfaced as a reason.
    """
    findings: list[str] = []
    for content in material:
        findings.extend(scan_sensitive_text(content))
    if findings:
        return PolicyDecision(
            False, tuple(f"detected:{name}" for name in dict.fromkeys(findings))
        )
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
    if request.model.provider not in authorization.authorized_provider_ids:
        reasons.append("provider is outside the authorization")
    if request.model.provider not in plan.provider_ids:
        reasons.append("provider is outside the plan")
    expected_models = (
        (task.implementation_model, task.fallback_model)
        if request.role in ("implementation", "revision")
        else (task.review_model, task.fallback_model)
        if request.role in ("review", "rereview")
        else ()
    )
    if request.model not in expected_models:
        reasons.append("model is not authorized for this task role")
    if request.model.registry_key in plan.blocked_model_keys:
        reasons.append("model is blocked by the plan")
    if plan.allowed_model_keys and request.model.registry_key not in plan.allowed_model_keys:
        reasons.append("model is outside the plan allowlist")
    if estimated_remote_cost < 0:
        reasons.append("estimated cost cannot be negative")
    if request.data_sensitivity is not task.data_sensitivity:
        reasons.append("privacy denied: request sensitivity differs from task contract")
    if not request.model.is_local:
        is_review_role = request.role in ("review", "rereview")
        is_worker_role = request.role in ("implementation", "revision")
        if is_worker_role:
            if not task.allow_remote_implementation:
                reasons.append(
                    "remote role denied: remote implementation is not authorized for this task"
                )
            elif request.read_only:
                reasons.append("remote role denied: implementation must be a write role")
            if task.remote_worker_network_mode is RemoteNetworkMode.ALLOWLIST:
                reasons.append(
                    "network denied: the remote worker host allowlist cannot be "
                    "enforced by the OpenCode permission layer"
                )
        elif is_review_role:
            if not request.read_only:
                reasons.append("remote role denied: reviewer must be read-only")
        else:
            reasons.append(f"remote role denied: unsupported remote role {request.role!r}")
        if plan.budget_mode is BudgetMode.LOCAL_FREE:
            reasons.append("budget denied: local-free mode prohibits remote models")
        usable_budget = max(0.0, authorization.max_remote_cost - plan.emergency_reserve)
        if remote_cost_spent + estimated_remote_cost > usable_budget:
            reasons.append("budget denied: remote cost would exceed the usable budget")
        privacy = remote_data_decision(
            request.data_sensitivity,
            authorization=authorization,
            redaction_passed=redaction_passed,
            content=request.prompt,
        )
        reasons.extend(f"privacy denied: {reason}" for reason in privacy.reasons)
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
    ):
        if not implementation.family or not reviewer.family:
            return PolicyDecision(
                False,
                ("important tasks require explicit model family metadata",),
            )
        if implementation.family_key == reviewer.family_key:
            return PolicyDecision(
                False,
                ("important tasks require a different model family",),
            )
    return PolicyDecision(True)


def review_contributor_independence(
    task: TaskContract, reviewer, contributors: Iterable
) -> PolicyDecision:
    """Reject a reviewer that actually contributed to the current artifact.

    Contributors are read from persisted ``COMPLETED`` implementation/revision
    calls, so fallback and continuation models are included. The same-model rule
    always applies; important tasks additionally require a different family.
    """
    for contributor in contributors:
        if contributor.registry_key == reviewer.registry_key:
            return PolicyDecision(
                False,
                ("a reviewer cannot also have contributed to the artifact",),
            )
        if task.risk_level.business_importance in (
            BusinessImportance.IMPORTANT,
            BusinessImportance.CRITICAL,
        ):
            if not reviewer.family or not contributor.family:
                return PolicyDecision(
                    False,
                    ("important tasks require explicit model family metadata",),
                )
            if reviewer.family_key == contributor.family_key:
                return PolicyDecision(
                    False,
                    ("important tasks require a different model family",),
                )
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
