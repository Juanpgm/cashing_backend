"""Domain exceptions with HTTP status code mapping."""

from fastapi import HTTPException, status

# --- Structured error codes ---
#
# Additive, machine-readable companions to `detail` (which stays free text).
# Frontend recovery paths should match on `code` instead of sniffing `detail`
# or the HTTP status code.
ACTIVIDADES_MISSING = "ACTIVIDADES_MISSING"
# Replaces GOOGLE_NOT_CONNECTED (microsoft-365-integration Slice C2, Reconciliation
# Note #1: no alias kept — the gate is provider-agnostic now, google-specific code
# is gone).
NO_PROVIDER_CONNECTED = "NO_PROVIDER_CONNECTED"
CHECKLIST_INCOMPLETE = "CHECKLIST_INCOMPLETE"
# Pre-radicación coherence validator (billing-resilience-templates, slice #1): one or
# more HARD findings from `coherence_validator_service` block `radicar_cuenta`.
COHERENCE_CHECK_FAILED = "COHERENCE_CHECK_FAILED"
# Evidence packager hardening (billing-resilience-templates, slice #2): the mandatory
# fail-closed secret scan found a hit in `generar_zip_evidencias` — no zip is emitted.
SECRET_DETECTED_IN_PACKAGE = "SECRET_DETECTED_IN_PACKAGE"
# `generar_zip_evidencias(modo="final")` was attempted with one or more obligaciones
# still PENDIENTE (no evidence) — the packager refuses to finalize an incomplete package.
PACKAGE_PENDIENTE = "PACKAGE_PENDIENTE"
# Cuota position model (billing-resilience-templates, slice #3): a write would produce
# an inconsistent position for a contract (two cuotas both `informe_final=true`, or a
# second `posicion=primera` for the same contrato).
CUOTA_POSITION_CONFLICT = "CUOTA_POSITION_CONFLICT"
# Explicit `numero_cuota` override (cuota-numero-explicito): the requested number
# collides with another active (non-deleted) cuota of the same contrato.
CUOTA_NUMERO_CONFLICT = "CUOTA_NUMERO_CONFLICT"


class DomainError(Exception):
    """Base domain error."""

    def __init__(self, detail: str = "An error occurred", code: str | None = None) -> None:
        self.detail = detail
        self.code = code
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


class InviteRequiredError(DomainError):
    """Waitlist gate is enabled and a valid invite code was not provided."""

    def __init__(self, detail: str = "Se requiere un código de invitación válido para registrarse.") -> None:
        super().__init__(detail)


class ExternalServiceError(DomainError):
    """External API call failed."""

    def __init__(self, service: str = "External service", detail: str = "unavailable", code: str | None = None) -> None:
        super().__init__(f"{service}: {detail}", code=code)


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
    InviteRequiredError: status.HTTP_403_FORBIDDEN,
}


def domain_to_http(exc: DomainError) -> HTTPException:
    """Convert a domain exception to an HTTPException."""
    status_code = EXCEPTION_STATUS_MAP.get(type(exc), status.HTTP_500_INTERNAL_SERVER_ERROR)
    return HTTPException(status_code=status_code, detail=exc.detail)
