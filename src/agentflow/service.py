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

        row, outcome = self.database.begin_call(
            request, context.attempt_id, reuse_planned=reuse_planned
        )
        if outcome == "PAUSE_BLOCKED":
            raise PauseBlockedError("run is no longer running; call was not started")
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
                self.database.transition_call(call_id, InvocationState.UNKNOWN)
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
            raw_metadata={"reused": True},
            cost_unavailable=bool(row["cost_unavailable"]),
        )

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
