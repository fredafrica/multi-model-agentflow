"""Issue and validate plan-bound authorization snapshots."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .contracts import AuthorizationSnapshot, PlanContract
from .serialization import plan_hash


DEFAULT_AUTHORIZATION_LIFETIME = timedelta(hours=24)


def issue_authorization(
    plan: PlanContract,
    *,
    allow_d1_remote: bool = False,
    allow_d2_remote: bool = False,
    now: datetime | None = None,
) -> AuthorizationSnapshot:
    issued_at = now or datetime.now(timezone.utc)
    files = tuple(sorted({path for task in plan.tasks for path in task.allowed_files}))
    models = tuple(
        sorted(
            {
                model.registry_key
                for task in plan.tasks
                for model in (
                    task.implementation_model,
                    task.review_model,
                    task.fallback_model,
                )
                if model is not None
            }
        )
    )
    return AuthorizationSnapshot(
        authorization_id=str(uuid4()),
        plan_id=plan.plan_id,
        plan_version=plan.version,
        plan_hash=plan_hash(plan),
        authorized_at=issued_at,
        expires_at=issued_at + DEFAULT_AUTHORIZATION_LIFETIME,
        authorized_task_ids=tuple(task.task_id for task in plan.tasks),
        authorized_model_keys=models,
        allowed_files=files,
        max_remote_cost=plan.max_remote_cost,
        run_mode=plan.run_mode,
        data_policy={
            "allow_d1_remote": allow_d1_remote,
            "allow_d2_remote": allow_d2_remote,
        },
        max_retry_count=max((task.max_retry_count for task in plan.tasks), default=0),
        stop_conditions=("authorization_expired", "budget_exhausted", "policy_violation"),
        escalation_conditions=tuple(
            sorted({item for task in plan.tasks for item in task.escalation_conditions})
        ),
    )


def validate_authorization(
    authorization: AuthorizationSnapshot,
    plan: PlanContract,
    *,
    now: datetime | None = None,
) -> None:
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise ValueError("authorization checks require a timezone-aware datetime")
    if authorization.expires_at <= checked_at:
        raise ValueError("authorization expired")
    if authorization.plan_id != plan.plan_id or authorization.plan_version != plan.version:
        raise ValueError("authorization targets a different plan version")
    if authorization.plan_hash != plan_hash(plan):
        raise ValueError("plan content changed after authorization")
    if authorization.run_mode != plan.run_mode:
        raise ValueError("run mode changed after authorization")
