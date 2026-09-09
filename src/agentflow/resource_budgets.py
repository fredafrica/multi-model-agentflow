"""Plan-frozen resource budgets; no provider defaults or mutable registry lookup."""
from .contracts import ModelCapabilitySnapshot, ModelRecord, ModelRef, PlanContract, TaskContract
from .serialization import digest_sha256, to_primitive, capability_from_mapping
from .adapters import ReviewerUnavailableError


class OutputBudgetUnavailable(ReviewerUnavailableError, ValueError):
    """A bounded model request cannot be constructed from the supplied evidence."""


def freeze_capability(record: ModelRecord) -> ModelCapabilitySnapshot:
    """Freeze explicit registry/catalog evidence before displaying and authorizing a plan."""
    if record.max_output_tokens is None or record.capability_source is None:
        raise ValueError("model output capability and source are unknown")
    return ModelCapabilitySnapshot(record.ref, record.context_length, record.max_output_tokens,
                                   record.capability_source, record.capability_source_version, record.api_model_id)


def invocation_budgets(plan: PlanContract, task: TaskContract, model: ModelRef, role: str) -> dict:
    review = role in ("review", "rereview")
    if not review and role not in ("implementation", "revision"):
        raise ValueError("unsupported budget role")
    steps = task.review_max_steps if review else (
        task.implementation_max_steps if model.is_local else task.remote_worker_max_steps)
    authorized = task.review_max_output_tokens if review else task.implementation_max_output_tokens
    capability = next((item for item in plan.model_capabilities if item.ref == model), None)
    return {
        "role": role, "model": to_primitive(model), "configured_steps": steps,
        "authorized_max_output_tokens": authorized,
        "capability": to_primitive(capability),
        "capability_id": digest_sha256(capability) if capability else None,
        "effective_max_output_tokens": min(authorized, capability.max_output_tokens) if capability else None,
        "policy": "frozen_min_reject_decrease_v1" if capability else "unknown_pause",
    }


def validate_invocation_budgets(request) -> dict:
    """Validate untrusted adapter metadata before starting any process."""
    from .contracts import TOKEN_LIMIT
    budget = request.metadata.get("resource_budgets")
    if not isinstance(budget, dict) or budget.get("capability") is None:
        raise OutputBudgetUnavailable("output capability snapshot is required before inference")
    try:
        capability = capability_from_mapping(budget["capability"])
    except (KeyError, TypeError, ValueError) as error:
        raise OutputBudgetUnavailable("invalid output capability snapshot") from error
    authorized = budget.get("authorized_max_output_tokens")
    effective = budget.get("effective_max_output_tokens")
    if type(authorized) is not int or not 1 <= authorized <= TOKEN_LIMIT:
        raise ValueError("invalid authorized output budget")
    if type(effective) is not int or effective != min(authorized, capability.max_output_tokens):
        raise ValueError("invalid effective output budget")
    if (capability.ref != request.model or budget.get("model") != to_primitive(request.model)
            or budget.get("role") != request.role or budget.get("capability_id") != digest_sha256(capability)
            or budget.get("policy") != "frozen_min_reject_decrease_v1"):
        raise ValueError("output capability identity or policy mismatch")
    return budget


def plan_budget_summary(plan: PlanContract) -> list[dict]:
    """Derived display only; the plan and its snapshots remain the authority."""
    result = []
    for task in plan.tasks:
        for role in ("implementation", "revision", "review", "rereview"):
            primary = task.review_model if role in ("review", "rereview") else task.implementation_model
            for model in dict.fromkeys((primary, task.fallback_model)):
                if model is not None:
                    result.append({"task_id": task.task_id, **invocation_budgets(plan, task, model, role)})
    return result
