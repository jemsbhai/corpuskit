"""Typed, public-safe application errors."""

from __future__ import annotations

from enum import StrEnum


class ApplicationErrorCode(StrEnum):
    """Stable machine-readable error identifiers."""

    INVALID_REQUEST = "invalid_request"
    LANGUAGE_NOT_SUPPORTED = "language_not_supported"
    INVENTORY_NOT_FOUND = "inventory_not_found"
    INVENTORY_DATA_UNAVAILABLE = "inventory_data_unavailable"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    ENGINE_UNAVAILABLE = "engine_unavailable"
    ENGINE_CONTRACT_VIOLATION = "engine_contract_violation"
    RESOURCE_NOT_FOUND = "resource_not_found"
    RESOURCE_CONFLICT = "resource_conflict"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"


class ApplicationError(RuntimeError):
    """Base application exception with a non-sensitive public message."""

    code: ApplicationErrorCode
    public_message: str

    def __init__(self, operation: str) -> None:
        self.operation = operation
        super().__init__(self.public_message)


class InvalidRequestError(ApplicationError):
    code = ApplicationErrorCode.INVALID_REQUEST
    public_message = "The request is not valid for this operation."


class LanguageNotSupportedError(ApplicationError):
    code = ApplicationErrorCode.LANGUAGE_NOT_SUPPORTED
    public_message = "The requested language is not supported."


class InventoryNotFoundError(ApplicationError):
    code = ApplicationErrorCode.INVENTORY_NOT_FOUND
    public_message = "No matching phoneme inventory was found."


class InventoryDataUnavailableError(ApplicationError):
    code = ApplicationErrorCode.INVENTORY_DATA_UNAVAILABLE
    public_message = "The phoneme inventory data is not available."


class DependencyUnavailableError(ApplicationError):
    code = ApplicationErrorCode.DEPENDENCY_UNAVAILABLE
    public_message = "A required language-processing dependency is not available."


class EngineUnavailableError(ApplicationError):
    code = ApplicationErrorCode.ENGINE_UNAVAILABLE
    public_message = "The corpus processing engine could not complete the operation."


class EngineContractError(ApplicationError):
    code = ApplicationErrorCode.ENGINE_CONTRACT_VIOLATION
    public_message = "The corpus processing engine returned an incompatible result."


class ResourceNotFoundError(ApplicationError):
    """A tenant-scoped resource is missing or inaccessible."""

    code = ApplicationErrorCode.RESOURCE_NOT_FOUND
    public_message = "The requested resource was not found."


class ResourceConflictError(ApplicationError):
    """A uniqueness or immutable-version constraint prevents the operation."""

    code = ApplicationErrorCode.RESOURCE_CONFLICT
    public_message = "The requested resource conflicts with existing data."


class InvalidStateTransitionError(ApplicationError):
    """A run state change violates the durable lifecycle contract."""

    code = ApplicationErrorCode.INVALID_STATE_TRANSITION
    public_message = "The requested run state transition is not allowed."


class QuotaExceededError(ApplicationError):
    """A server-owned tenant resource ceiling rejects new work."""

    code = ApplicationErrorCode.QUOTA_EXCEEDED
    public_message = "The organization resource quota is currently exhausted."

    def __init__(self, operation: str, *, retry_after_seconds: int | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(operation)


class TrafficRateLimitExceededError(ApplicationError):
    """A centralized tenant/subject/route traffic ceiling rejected the request."""

    code = ApplicationErrorCode.RATE_LIMITED
    public_message = "The request rate limit is temporarily exhausted."

    def __init__(self, operation: str, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(operation)
