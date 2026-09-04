"""Deterministic, no-network adapter for tests and dry runs."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .contracts import InvocationRequest, InvocationResult, ModelRecord


class FakeAdapter:
    adapter_id = "fake"

    def __init__(
        self,
        models: Sequence[ModelRecord] = (),
        responder: Callable[[InvocationRequest], InvocationResult] | None = None,
    ) -> None:
        self._models = tuple(models)
        self._responder = responder or self._default_response
        self.invocations: list[InvocationRequest] = []
        self.results: dict[str, InvocationResult] = {}

    def discover(self) -> Sequence[ModelRecord]:
        return self._models

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        self.invocations.append(request)
        result = self._responder(request)
        self.results[result.provider_request_id or request.request_key] = result
        return result

    def query(self, provider_request_id: str) -> InvocationResult | None:
        return self.results.get(provider_request_id)

    def cancel(self, provider_request_id: str) -> bool:
        return provider_request_id in self.results

    @staticmethod
    def _default_response(request: InvocationRequest) -> InvocationResult:
        return InvocationResult(
            provider_request_id=f"fake:{request.request_key}",
            output="fake result",
            input_tokens=0,
            output_tokens=0,
            first_token_latency_ms=0,
            duration_ms=0,
            remote_cost=0,
            raw_metadata={"test_double": True},
        )
