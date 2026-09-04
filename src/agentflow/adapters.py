"""Provider-neutral adapter protocol used by the deterministic core."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, Sequence

from .contracts import InvocationRequest, InvocationResult, ModelRecord


class InvocationOutcomeUnknown(RuntimeError):
    """The request may have reached the provider, so retrying could duplicate cost."""

    def __init__(self, message: str, provider_request_id: str | None = None) -> None:
        super().__init__(message)
        self.provider_request_id = provider_request_id


class ModelAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    def discover(self) -> Sequence[ModelRecord]: ...

    def invoke(self, request: InvocationRequest) -> InvocationResult: ...

    def query(self, provider_request_id: str) -> InvocationResult | None: ...

    def cancel(self, provider_request_id: str) -> bool: ...


class AdapterRouter:
    adapter_id = "router"

    def __init__(self, adapters: Mapping[str, ModelAdapter]) -> None:
        self.adapters = dict(adapters)

    def adapter_for(self, provider: str) -> ModelAdapter:
        try:
            return self.adapters[provider]
        except KeyError as error:
            raise ValueError(f"no adapter registered for provider: {provider}") from error

    def discover(self) -> Sequence[ModelRecord]:
        return tuple(record for adapter in self.adapters.values() for record in adapter.discover())

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        return self.adapter_for(request.model.provider).invoke(request)

    def query(self, provider_request_id: str) -> InvocationResult | None:
        for adapter in self.adapters.values():
            result = adapter.query(provider_request_id)
            if result is not None:
                return result
        return None

    def query_provider(
        self, provider: str, provider_request_id: str
    ) -> InvocationResult | None:
        return self.adapter_for(provider).query(provider_request_id)

    def cancel(self, provider_request_id: str) -> bool:
        return any(adapter.cancel(provider_request_id) for adapter in self.adapters.values())
