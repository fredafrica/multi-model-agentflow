"""Deterministic invocation coordinator shared by CLI and Skill entry points."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .adapters import (
    AdapterRouter,
    InvocationIncompleteError,
    InvocationOutcomeUnknown,
    ModelAdapter,
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
from .policies import invocation_decision
from .states import InvocationState


class PolicyDeniedError(RuntimeError):
    pass


class ConfirmationRequiredError(RuntimeError):
    pass


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
    ) -> InvocationResult:
        validate_authorization(context.authorization, context.plan)
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
            raise PolicyDeniedError("; ".join(decision.reasons))
        if decision.requires_confirmation and not confirmed:
            raise ConfirmationRequiredError("this invocation requires explicit confirmation")

        row, created = self.database.register_call(request, context.attempt_id)
        if not created:
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
                raw_metadata={"reused": True},
                cost_unavailable=bool(row["cost_unavailable"]),
            )

        self.database.transition_call(request.call_id, InvocationState.STARTED)
        try:
            result = self.adapter.invoke(request)
        except InvocationIncompleteError as error:
            result = error.result
            if request.model.is_local:
                result = replace(result, remote_cost=0.0, cost_unavailable=False)
                error.result = result
            self.database.fail_call(
                request.call_id,
                result,
                request.run_id,
                failure_kind=error.failure_kind,
            )
            raise
        except InvocationOutcomeUnknown as error:
            if error.provider_request_id:
                self.database.set_provider_request_id(request.call_id, error.provider_request_id)
            self.database.transition_call(request.call_id, InvocationState.UNKNOWN)
            raise
        except Exception:
            self.database.transition_call(request.call_id, InvocationState.FAILED)
            raise
        if request.model.is_local:
            result = replace(result, remote_cost=0.0, cost_unavailable=False)
        self.database.complete_call(request.call_id, result, request.run_id)
        return result

    def resolve_unknown(self, run_id: str, call_id: str) -> InvocationResult | None:
        row = self.database.unknown_call(run_id, call_id)
        provider_request_id = row["provider_request_id"]
        if not provider_request_id:
            return None
        if isinstance(self.adapter, AdapterRouter):
            result = self.adapter.query_provider(
                str(row["provider"]), str(provider_request_id)
            )
        else:
            result = self.adapter.query(str(provider_request_id))
        if result is None:
            return None
        self.database.complete_call(call_id, result, run_id)
        return result
