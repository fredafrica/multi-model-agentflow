"""Deterministic invocation coordinator shared by CLI and Skill entry points."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json

from .adapters import (
    AdapterRouter,
    InvocationIncompleteError,
    InvocationProtocolError,
    InvocationOutcomeUnknown,
    ModelAdapter,
    ReviewerProtocolError,
    WorkerProtocolError,
)
from .authorization import validate_authorization
from .contracts import (
    AuthorizationSnapshot,
    InvocationRequest,
    InvocationResult,
    PlanContract,
    TaskContract,
)
from .database import Database
from .policies import classify_policy_denial, invocation_decision
from .states import InvocationState
from .resource_budgets import invocation_budgets
from .serialization import canonical_json


class PolicyDeniedError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        reason_code: str = "authorization_violation",
        reasons: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.reasons = tuple(reasons)


class ConfirmationRequiredError(RuntimeError):
    pass


class PauseBlockedError(RuntimeError):
    """The run was no longer RUNNING when a call tried to start."""


@dataclass(frozen=True)
class InvocationContext:
    plan: PlanContract
    task: TaskContract
    authorization: AuthorizationSnapshot
    attempt_id: str
    estimated_remote_cost: float = 0
    redaction_passed: bool = False


class InvocationService:
    def __init__(self, database: Database, adapter: ModelAdapter) -> None:
        self.database = database
        self.adapter = adapter

    def invoke(
        self,
        request: InvocationRequest,
        context: InvocationContext,
        *,
        confirmed: bool = False,
        reuse_planned: bool = False,
    ) -> InvocationResult:
        validate_authorization(context.authorization, context.plan)
        if context.task not in context.plan.tasks:
            raise PolicyDeniedError("task contract differs from authorized plan")
        budgets = invocation_budgets(context.plan, context.task, request.model, request.role)
        supplied = request.metadata.get("resource_budgets")
        if supplied is not None and canonical_json(supplied) != canonical_json(budgets):
            raise PolicyDeniedError("request resource budgets differ from authorized plan")
        if "review_max_steps" in request.metadata and (
            type(request.metadata["review_max_steps"]) is not int
            or request.metadata["review_max_steps"] != context.task.review_max_steps
        ):
            raise PolicyDeniedError("request review steps differ from authorized plan")
        request = replace(request, metadata={**request.metadata, "resource_budgets": budgets,
                                            "review_max_steps": context.task.review_max_steps})
        decision = invocation_decision(
            request,
            task=context.task,
            plan=context.plan,
            authorization=context.authorization,
            estimated_remote_cost=context.estimated_remote_cost,
            remote_cost_spent=self.database.remote_budget_committed(request.run_id),
            redaction_passed=context.redaction_passed,
        )
        if not decision.allowed:
            self.database.record_policy_denial(
                request.run_id,
                request.call_id,
                request.task_id,
                request.model.registry_key,
                decision.reasons,
            )
            raise PolicyDeniedError(
                "; ".join(decision.reasons),
                reason_code=classify_policy_denial(decision.reasons),
                reasons=decision.reasons,
            )
        if decision.requires_confirmation and not confirmed:
            raise ConfirmationRequiredError("this invocation requires explicit confirmation")

        if request.model.is_local:
            budget_estimated: float | None = None
            budget_usable: float | None = None
        else:
            budget_estimated = context.estimated_remote_cost
            budget_usable = max(
                0.0,
                context.authorization.max_remote_cost - context.plan.emergency_reserve,
            )
        row, outcome = self.database.begin_call(
            request,
            context.attempt_id,
            reuse_planned=reuse_planned,
            estimated_remote_cost=budget_estimated,
            usable_budget=budget_usable,
        )
        if outcome == "PAUSE_BLOCKED":
            raise PauseBlockedError("run is no longer running; call was not started")
        if outcome == "BUDGET_BLOCKED":
            reasons = ("budget denied: remote cost would exceed the usable budget",)
            self.database.record_policy_denial(
                request.run_id,
                request.call_id,
                request.task_id,
                request.model.registry_key,
                reasons,
            )
            raise PolicyDeniedError(
                "; ".join(reasons),
                reason_code=classify_policy_denial(reasons),
                reasons=reasons,
            )
        if outcome == "REUSED_COMPLETED":
            return self._reused_result(row)

        call_id = str(row["call_id"])
        if call_id != request.call_id:
            metadata = dict(request.metadata)
            metadata["on_provider_request_id"] = (
                lambda provider_request_id, call_id=call_id: (
                    self.database.set_provider_request_id(
                        call_id, provider_request_id
                    )
                )
            )
            request = replace(request, call_id=call_id, metadata=metadata)

        try:
            result = self.adapter.invoke(request)
        except InvocationIncompleteError as error:
            result = error.result
            if request.model.is_local:
                result = replace(result, remote_cost=0.0, cost_unavailable=False)
                error.result = result
            self.database.fail_call(
                call_id,
                result,
                request.run_id,
                failure_kind=error.failure_kind,
            )
            raise
        except InvocationOutcomeUnknown as error:
            if error.result is not None:
                result = error.result
                if request.model.is_local:
                    result = replace(result, remote_cost=0.0, cost_unavailable=False)
                    error.result = result
                self.database.mark_call_unknown(
                    call_id, error.provider_request_id, result
                )
            else:
                if error.provider_request_id:
                    self.database.set_provider_request_id(
                        call_id, error.provider_request_id
                    )
                result = InvocationResult(
                    provider_request_id=error.provider_request_id,
                    output="",
                    input_tokens=0,
                    output_tokens=0,
                    first_token_latency_ms=None,
                    duration_ms=0,
                    remote_cost=0.0 if request.model.is_local else None,
                    raw_metadata={
                        "termination_reason": error.termination_reason or "unknown",
                        "token_source": "unavailable",
                        "usage_unavailable": True,
                    },
                )
                self.database.mark_call_unknown(
                    call_id, error.provider_request_id, result
                )
            raise
        except (InvocationProtocolError, ReviewerProtocolError, WorkerProtocolError) as error:
            result = error.result
            if result is not None:
                if request.model.is_local:
                    result = replace(result, remote_cost=0.0, cost_unavailable=False)
                self.database.fail_call(
                    call_id,
                    result,
                    request.run_id,
                    failure_kind="protocol_error",
                )
            else:
                self.database.transition_call(call_id, InvocationState.FAILED)
            raise
        except Exception:
            self.database.transition_call(call_id, InvocationState.FAILED)
            raise
        if request.model.is_local:
            result = replace(result, remote_cost=0.0, cost_unavailable=False)
        self.database.complete_call(call_id, result, request.run_id)
        return result

    @staticmethod
    def _reused_result(row) -> InvocationResult:
        return InvocationResult(
            provider_request_id=row["provider_request_id"],
            output=str(row["output_text"] or ""),
            input_tokens=int(row["input_tokens"] or 0),
            output_tokens=int(row["output_tokens"] or 0),
            first_token_latency_ms=row["first_token_latency_ms"],
            duration_ms=int(row["duration_ms"] or 0),
            remote_cost=(
                float(row["remote_cost"])
                if row["remote_cost"] is not None
                else None
            ),
            raw_metadata={**json.loads(row["raw_metadata_json"] or "{}"), "reused": True,
                          "resource_budgets": json.loads(row["request_scope_json"]).get("resource_budgets")},
            cost_unavailable=bool(row["cost_unavailable"]),
        )

    def resolve_unknown(self, run_id: str, call_id: str) -> InvocationResult | None:
        row = self.database.unknown_call(run_id, call_id)
        provider_request_id = row["provider_request_id"]
        if not provider_request_id:
            return None
        if isinstance(self.adapter, AdapterRouter):
            result = self.adapter.query_provider(
                str(row["provider"]), str(provider_request_id), str(row["role"])
            )
        else:
            result = self.adapter.query(str(provider_request_id))
        if result is None:
            return None
        if row["is_local"]:
            result = replace(result, remote_cost=0.0, cost_unavailable=False)
        self.database.complete_call(call_id, result, run_id)
        return result
