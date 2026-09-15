"""Domain exceptions with HTTP status code mapping."""

import uuid

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
# A cuenta de cobro already exists for the same (contrato, mes, anio) — lets the
# frontend show a friendly Spanish message instead of the raw English `detail`.
CUENTA_MES_DUPLICADA = "CUENTA_MES_DUPLICADA"
# The document itself was persisted, but linking it to its checklist requisito
# failed. Distinct from a plain upload failure: the client must not assume the
# file is lost — re-uploading the same content repairs the link (see
# document_service.upload_document's content-hash dedup fast path).
CHECKLIST_LINK_FAILED = "CHECKLIST_LINK_FAILED"
# `persistir_evidencias` (agent tool) was called with a `handle_id` that is
# unknown, expired, or does not match the caller/cuenta_id it was issued for
# (radicacion-sin-friccion 1.7 — see `app.services.evidence_handle_cache`).
EVIDENCE_HANDLE_NOT_FOUND = "EVIDENCE_HANDLE_NOT_FOUND"
# A write-tool approval/cancel/retry control endpoint (radicacion-sin-friccion 3.10,
# `POST /api/v1/agent/chat/stream/{session_id}/tool-calls/{call_id}/...`) was called
# with a `call_id` that is unknown, expired, or belongs to another user's session
# (see `app.services.agent_tool_approval`).
PENDING_TOOL_CALL_NOT_FOUND = "PENDING_TOOL_CALL_NOT_FOUND"
# Package generation as a poll-based background job, per-cuenta lock
# (radicacion-sin-friccion, Phase 2 slice 2.7): a synchronous `POST
# /paquete/regenerar` call lost the per-cuenta lock to an already in-flight run
# (sync OR async) — see `app.services.paquete_job_service._upsert_job`.
PAQUETE_GENERACION_EN_CURSO = "PAQUETE_GENERACION_EN_CURSO"


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


class EvidenceHandleNotFoundError(NotFoundError):
    """`persistir_evidencias` was called with a discovery handle that can't be redeemed.

    Deliberately ONE outcome/message for four distinct causes — unknown handle,
    expired handle, handle owned by a different user, and handle scoped to a
    different `cuenta_id` — same never-leak-existence rationale documented on
    `evidence_persist_service._verify_cuenta_owned` and
    `app/tools/catalog/importar_documento.py`: telling the caller WHICH of those
    it hit would leak information about a handle they don't own. The message is
    still actionable (unlike a bare 404) because every cause has the exact same
    fix — call `descubrir_evidencias` again for a fresh handle — so collapsing
    the cases costs nothing.

    Maps to HTTP 404 via `NotFoundError`'s entry in `EXCEPTION_STATUS_MAP`
    (MRO-based lookup, see `domain_to_http`) — no separate registration needed.
    """

    def __init__(self) -> None:
        DomainError.__init__(
            self,
            "El handle de descubrimiento de evidencias no existe, expiró o no corresponde a "
            "esta cuenta. Volvé a llamar a descubrir_evidencias para generar uno nuevo antes "
            "de persistir.",
            code=EVIDENCE_HANDLE_NOT_FOUND,
        )


class PendingToolCallNotFoundError(NotFoundError):
    """A tool-call control action (approve/reject/cancel/retry) targeted a `call_id`
    that can't be resolved for the calling user.

    Deliberately ONE outcome/message for three distinct causes — unknown call_id,
    expired pending entry, and a call_id that exists but belongs to a different
    user's session — same never-leak-existence rationale as
    `EvidenceHandleNotFoundError`: distinguishing "doesn't exist" from "exists but
    isn't yours" would let user B probe for user A's session activity.

    Maps to HTTP 404 via `NotFoundError`'s entry in `EXCEPTION_STATUS_MAP`
    (MRO-based lookup, see `domain_to_http`) — no separate registration needed.
    """

    def __init__(self) -> None:
        DomainError.__init__(
            self,
            "Esta acción sobre la herramienta no existe, expiró, o no corresponde a esta sesión de chat.",
            code=PENDING_TOOL_CALL_NOT_FOUND,
        )


class AlreadyExistsError(DomainError):
    """Resource already exists."""

    def __init__(self, resource: str = "Resource", field: str = "", code: str | None = None) -> None:
        detail = f"{resource} already exists"
        if field:
            detail = f"{resource} with this {field} already exists"
        super().__init__(detail, code=code)


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


class ChecklistLinkError(DomainError):
    """The document was uploaded and persisted, but linking it to a checklist
    requisito failed. Re-uploading the same file content re-attempts the link
    (see the content-hash dedup fast path in document_service.upload_document)."""

    def __init__(self, requisito_codigo: str, reason: str, archivos: list[str] | None = None) -> None:
        if archivos:
            # Batch flavour: name the affected files so the client knows exactly
            # which ones need a retry (the others in the batch are fine).
            nombres = ", ".join(f"'{a}'" for a in archivos)
            sujeto = "Los archivos" if len(archivos) > 1 else "El archivo"
            verbo = "se guardaron" if len(archivos) > 1 else "se guardó"
            pudieron = "pudieron" if len(archivos) > 1 else "pudo"
            detail = (
                f"{sujeto} {nombres} {verbo} correctamente pero no {pudieron} vincularse al requisito "
                f"'{requisito_codigo}': {reason}. Volvé a subir el mismo archivo para reintentar la vinculación."
            )
        else:
            detail = (
                f"El archivo se guardó correctamente pero no pudo vincularse al requisito "
                f"'{requisito_codigo}': {reason}. Volvé a subir el mismo archivo para reintentar la vinculación."
            )
        super().__init__(detail, code=CHECKLIST_LINK_FAILED)


class PaqueteGenerationInProgressError(DomainError):
    """A synchronous `POST /paquete/regenerar` call lost the per-cuenta job
    lock to an already in-flight run (radicacion-sin-friccion, Phase 2 slice
    2.7 — see `app.services.paquete_job_service`'s module docstring for the
    Option A/B design decision this implements).

    Deliberately does NOT silently run the expensive pipeline a second time in
    parallel (checklist + coherence + 2 LLM calls + zip + upload), and
    deliberately does NOT block-and-wait for the in-flight run inside this
    request either (a bounded wait-loop would add real latency/complexity for
    a genuinely rare interleaving). Fails fast with a clear, actionable 409
    instead: the caller can poll `GET /paquete/job` for the in-flight run's
    outcome, or simply retry `POST /paquete/regenerar` once it's done."""

    def __init__(self, cuenta_id: uuid.UUID) -> None:
        # No raw `cuenta_id` or internal endpoint path in the message — every
        # sibling DomainError in this file speaks to the end user in plain
        # Spanish (see `ChecklistLinkError` just above); this one originally
        # didn't (flagged by review), so it's kept consistent with the rest.
        #
        # Byte-identical to `PAQUETE_GENERACION_EN_CURSO`'s entry in the
        # frontend's `CODE_MESSAGES` (components/stepper/step-messages.ts) —
        # deliberately, not by coincidence (round-2 review follow-up): that
        # screen's existing dedup guard only suppresses this `detail` as a
        # redundant second line when it equals the already-rendered
        # translated message; a punctuation mismatch between the two would
        # make the guard miss and stack two near-identical sentences.
        super().__init__(
            "Ya hay una generación de paquete en curso — esperá unos segundos y volvé a intentar.",
            code=PAQUETE_GENERACION_EN_CURSO,
        )


_ATRIBUTO_DOCUMENTO_PERSISTIDO = "_documento_persistido"


def marcar_documento_persistido(exc: DomainError) -> None:
    """Flag a `DomainError` raised AFTER the document was already committed.

    `document_service.upload_document` commits the document and only then
    attempts the checklist link, so a `DomainError` escaping that block (a
    `ChecklistLinkError` or any other, e.g. a `ValidationError` from
    `vincular_documento_fuente`) belongs to a file that IS saved. Batch callers
    must classify it as unlinked, never as "no se guardó" — the difference
    decides whether the user retries the upload or only the link.
    """
    setattr(exc, _ATRIBUTO_DOCUMENTO_PERSISTIDO, True)


def documento_fue_persistido(exc: BaseException) -> bool:
    """Whether `exc` was flagged by `marcar_documento_persistido`."""
    return bool(getattr(exc, _ATRIBUTO_DOCUMENTO_PERSISTIDO, False))


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
    ChecklistLinkError: status.HTTP_502_BAD_GATEWAY,
    PaqueteGenerationInProgressError: status.HTTP_409_CONFLICT,
}


def domain_to_http(exc: DomainError) -> HTTPException:
    """Convert a domain exception to an HTTPException.

    Resolves the HTTP status by walking `type(exc).__mro__` (most specific
    class first) and using the first class found in `EXCEPTION_STATUS_MAP`.
    This means an unregistered subclass of an already-mapped domain
    exception (e.g. a new `ValidationError` subclass) inherits its mapped
    ancestor's status instead of silently falling back to 500. Falls back to
    500 only when nothing in the MRO is registered.
    """
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    for exc_cls in type(exc).__mro__:
        mapped_status = EXCEPTION_STATUS_MAP.get(exc_cls)
        if mapped_status is not None:
            status_code = mapped_status
            break
    return HTTPException(status_code=status_code, detail=exc.detail)
