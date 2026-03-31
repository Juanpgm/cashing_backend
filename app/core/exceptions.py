"""Domain exceptions with HTTP status code mapping."""

from fastapi import HTTPException, status


class DomainError(Exception):
    """Base domain error."""

    def __init__(self, detail: str = "An error occurred") -> None:
        self.detail = detail
        super().__init__(detail)


class NotFoundError(DomainError):
    """Resource not found."""

    def __init__(self, resource: str = "Resource", identifier: str = "") -> None:
        detail = f"{resource} not found"
        if identifier:
            detail = f"{resource} '{identifier}' not found"
        super().__init__(detail)


class AlreadyExistsError(DomainError):
    """Resource already exists."""

    def __init__(self, resource: str = "Resource", field: str = "") -> None:
        detail = f"{resource} already exists"
        if field:
            detail = f"{resource} with this {field} already exists"
        super().__init__(detail)


class ValidationError(DomainError):
    """Business rule validation failed."""


class InsufficientCreditsError(DomainError):
    """User doesn't have enough credits."""

    def __init__(self, required: int = 0, available: int = 0) -> None:
        detail = f"Insufficient credits: {available} available, {required} required"
        super().__init__(detail)


class UnauthorizedError(DomainError):
    """Authentication failed."""

    def __init__(self, detail: str = "Invalid credentials") -> None:
        super().__init__(detail)


class ForbiddenError(DomainError):
    """Authorization failed — user lacks permission."""

    def __init__(self, detail: str = "You don't have permission to access this resource") -> None:
        super().__init__(detail)


class RateLimitExceededError(DomainError):
    """Rate limit exceeded."""

    def __init__(self, detail: str = "Too many requests. Please try again later.") -> None:
        super().__init__(detail)


class ExternalServiceError(DomainError):
    """External API call failed."""

    def __init__(self, service: str = "External service", detail: str = "unavailable") -> None:
        super().__init__(f"{service}: {detail}")


# --- HTTP Exception mapping ---

EXCEPTION_STATUS_MAP: dict[type[DomainError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    AlreadyExistsError: status.HTTP_409_CONFLICT,
    ValidationError: status.HTTP_422_UNPROCESSABLE_ENTITY,
    InsufficientCreditsError: status.HTTP_402_PAYMENT_REQUIRED,
    UnauthorizedError: status.HTTP_401_UNAUTHORIZED,
    ForbiddenError: status.HTTP_403_FORBIDDEN,
    RateLimitExceededError: status.HTTP_429_TOO_MANY_REQUESTS,
    ExternalServiceError: status.HTTP_502_BAD_GATEWAY,
}


def domain_to_http(exc: DomainError) -> HTTPException:
    """Convert a domain exception to an HTTPException."""
    status_code = EXCEPTION_STATUS_MAP.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
    return HTTPException(status_code=status_code, detail=exc.detail)
