"""Provider-neutral adapter protocol used by the deterministic core."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, Sequence

from .contracts import InvocationRequest, InvocationResult, ModelRecord


class InvocationOutcomeUnknown(RuntimeError):
    """The request may have reached the provider, so retrying could duplicate cost."""

    def __init__(
        self,
        message: str,
        provider_request_id: str | None = None,
        *,
        result: InvocationResult | None = None,
        termination_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_request_id = provider_request_id
        self.result = result
        self.termination_reason = termination_reason


class InvocationIncompleteError(RuntimeError):
    """The provider returned a known, non-success terminal result with usage evidence."""

    def __init__(
        self,
        message: str,
        result: InvocationResult,
        *,
        failure_kind: str,
    ) -> None:
        super().__init__(message)
        self.result = result
        self.failure_kind = failure_kind


class ReviewerUnavailableError(RuntimeError):
    """A planned reviewer cannot be selected without sending a model request."""


class InvocationProtocolError(RuntimeError):
    """A completed process returned no usable result; keep its usage evidence."""

    def __init__(self, message: str, *, result: InvocationResult) -> None:
        super().__init__(message)
        self.result = result


class UnsupportedProviderError(ReviewerUnavailableError):
    pass


class ProviderNotConfiguredError(ReviewerUnavailableError):
    pass


class ModelUnavailableError(ReviewerUnavailableError):
    pass


class ReviewerProtocolError(ReviewerUnavailableError):
    """The reviewer answered, but its output did not satisfy the review protocol."""

    def __init__(
        self, message: str, *, result: InvocationResult | None = None
    ) -> None:
        super().__init__(message)
        self.result = result


class WorkerProtocolError(RuntimeError):
    """A remote worker answered, but its output did not satisfy the protocol.

    Unlike a pre-call unavailability this is a known post-call failure: the
    process already ran, so any confirmed token/cost evidence in ``result`` must
    be preserved and must not trigger a fallback re-dispatch.
    """

    def __init__(
        self, message: str, *, result: InvocationResult | None = None
    ) -> None:
        super().__init__(message)
        self.result = result


class ModelAdapter(Protocol):
    @property
    def adapter_id(self) -> str: ...

    def discover(self) -> Sequence[ModelRecord]: ...

    def invoke(self, request: InvocationRequest) -> InvocationResult: ...

    def query(self, provider_request_id: str) -> InvocationResult | None: ...

    def cancel(self, provider_request_id: str) -> bool: ...


class AdapterRouter:
    adapter_id = "router"

    def __init__(
        self,
        adapters: Mapping[str, ModelAdapter],
        role_adapters: Mapping[tuple[str, str], ModelAdapter] | None = None,
    ) -> None:
        self.adapters = dict(adapters)
        self.role_adapters = dict(role_adapters or {})

    def adapter_for(self, provider: str, role: str | None = None) -> ModelAdapter:
        if role is not None:
            key = (provider, role)
            if key in self.role_adapters:
                return self.role_adapters[key]
        if provider in self.adapters:
            return self.adapters[provider]
        # A provider registered only for specific roles (e.g. a local, review-only
        # Ollama reviewer) must not silently serve other roles such as an
        # implementation fallback: fail closed with a clean, catchable denial rather
        # than an opaque routing error.
        if role is not None and any(
            known_provider == provider for (known_provider, _role) in self.role_adapters
        ):
            raise ReviewerUnavailableError(
                f"provider {provider} is not available for role {role}"
            )
        raise ValueError(f"no adapter registered for provider: {provider}")

    def discover(self) -> Sequence[ModelRecord]:
        adapters = {id(adapter): adapter for adapter in self.adapters.values()}
        for adapter in self.role_adapters.values():
            adapters[id(adapter)] = adapter
        return tuple(
            record for adapter in adapters.values() for record in adapter.discover()
        )

    def invoke(self, request: InvocationRequest) -> InvocationResult:
        return self.adapter_for(request.model.provider, request.role).invoke(request)

    def query(self, provider_request_id: str) -> InvocationResult | None:
        seen: dict[int, ModelAdapter] = {}
        for adapter in self.adapters.values():
            seen[id(adapter)] = adapter
        for adapter in self.role_adapters.values():
            seen[id(adapter)] = adapter
        for adapter in seen.values():
            result = adapter.query(provider_request_id)
            if result is not None:
                return result
        return None

    def query_provider(
        self, provider: str, provider_request_id: str, role: str | None = None
    ) -> InvocationResult | None:
        return self.adapter_for(provider, role).query(provider_request_id)

    def cancel(self, provider_request_id: str) -> bool:
        seen: dict[int, ModelAdapter] = {}
        for adapter in self.adapters.values():
            seen[id(adapter)] = adapter
        for adapter in self.role_adapters.values():
            seen[id(adapter)] = adapter
        return any(adapter.cancel(provider_request_id) for adapter in seen.values())
