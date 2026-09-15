"""Shared transport-error mapping for the Google Workspace adapters (Gmail, Drive,
Calendar) — all three run blocking Google API calls via `run_in_executor` and
wrap `googleapiclient.errors.HttpError` (HTTP-level 4xx/5xx from the API itself)
into the domain `ExternalServiceError` (502) contract.

Beyond `HttpError`, several transport/auth failure modes can escape that
wrapping entirely and surface as a raw, unhandled 500 instead:

- `OSError` — raw socket/connection failures (`TimeoutError` is a subclass,
  listed explicitly below for readability rather than relying on the reader
  to recall the subclass relationship).
- `httplib2.ServerNotFoundError` — DNS resolution failure. NOT an `OSError`
  subclass (verified via `issubclass`), so a plain `except OSError` misses it.
- `google.auth.exceptions.TransportError` — transport failure during credential
  refresh. Also NOT an `OSError` subclass.

Found in radicacion-sin-friccion phase 4.3 review r1 (finding P2b): a Railway
DNS blip previously surfaced as a raw 500, never the 502 `ExternalServiceError`
contract the frontend's error banner relies on.

`google.auth.exceptions.RefreshError` is DELIBERATELY NOT in this tuple.
It can happen LAZILY inside a call to `.execute()`, not just in
`GmailAdapter.get_credentials`'s explicit `creds.refresh(...)` call, because
the underlying transport may auto-refresh an expired token on demand — review
r1 (finding P2b) added it here to close that raw-500 gap. But unlike a DNS
blip or a socket timeout, a `RefreshError` means the grant itself is revoked,
expired, or rotated: retrying does NOTHING until the user reconnects their
account. Classifying it as "transient transport outage" hid a permanent auth
failure behind a message that invites the user to just try again forever, with
no reconnect CTA (review r2, finding P2-1). Every call site that used to catch
`GOOGLE_TRANSPORT_ERRORS` (which included `RefreshError`) now ALSO catches
`RefreshError` separately, via `raise_google_reauth_required` below.
"""

from __future__ import annotations

from typing import NoReturn

import httplib2
import structlog
from google.auth.exceptions import TransportError

from app.core.exceptions import GOOGLE_REAUTH_REQUIRED, ExternalServiceError

GOOGLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    httplib2.ServerNotFoundError,
    TransportError,
)

# Same wording `GmailAdapter.get_credentials` already used on its explicit
# `creds.refresh(...)` path (radicacion-sin-friccion phase 4.3) — reused here
# so every `RefreshError` site (explicit refresh AND every lazy in-call
# refresh) shows the user the exact same actionable message (review r2,
# finding P2-1).
GOOGLE_REAUTH_MESSAGE = "Token vencido — reconectá tu cuenta de Google en /integraciones"


def raise_external_service_error(
    logger: structlog.BoundLogger,
    service: str,
    user_message: str,
    exc: BaseException,
    event: str,
    *,
    include_exc_type: bool = True,
    **log_context: object,
) -> NoReturn:
    """Log the raw exception via structlog, then raise `ExternalServiceError`
    with a generic, safe client-facing detail.

    `str(exc)` for a `googleapiclient.errors.HttpError` includes the FULL
    request URI — e.g. Gmail/Drive search `q=` terms, folder names, contract
    numbers — and `domain_error_handler` (`app.main`) returns
    `ExternalServiceError.detail` to the client VERBATIM, with no redaction
    (unlike the generic 500 path in `app.core.error_response`). The raw text
    is therefore logged here for diagnosis, never included in the raised
    exception's detail — which carries at most the exception's class name.

    `include_exc_type=False` for `GoogleHttpError`: its own class name is
    "HttpError", and the finding's own leak check forbids the substring
    "http" appearing anywhere in the client-facing detail (case-insensitive),
    so appending it would reintroduce the very leak this closes.

    Found in radicacion-sin-friccion phase 4.3 review r1 (finding P3a).

    NOTE: for a `GoogleHttpError`, prefer `raise_google_http_error` below —
    it carries the response status code and a per-class hint instead of
    collapsing every 4xx/5xx into one identical message (review r2, finding
    P2-2). This function stays the generic entry point for every non-HTTP
    failure mode (transport errors, malformed-payload parse errors, etc.).
    """
    logger.warning(event, error=str(exc), exc_type=type(exc).__name__, **log_context)
    detail = f"{user_message} ({type(exc).__name__})" if include_exc_type else user_message
    raise ExternalServiceError(service, detail) from exc


def raise_google_reauth_required(
    logger: structlog.BoundLogger,
    service: str,
    exc: BaseException,
    event: str,
    **log_context: object,
) -> NoReturn:
    """Log the raw `RefreshError`, then raise `ExternalServiceError` with the
    SAME reconnect-CTA message `GmailAdapter.get_credentials` already uses on
    its explicit-refresh path, plus the structured `GOOGLE_REAUTH_REQUIRED`
    code — so a frontend recovery path can match on `code` instead of sniffing
    `detail` (see `app.core.exceptions`'s code-catalog convention).

    Never includes `str(exc)` in the raised detail — same leak-prevention
    rule as `raise_external_service_error` (review r1, finding P3a): the
    message is fully canned, so there is nothing to leak.

    Found in radicacion-sin-friccion phase 4.3 review r2, finding P2-1.
    """
    logger.warning(event, error=str(exc), exc_type=type(exc).__name__, **log_context)
    raise ExternalServiceError(service, GOOGLE_REAUTH_MESSAGE, code=GOOGLE_REAUTH_REQUIRED) from exc


# Per-status-code Spanish hint, appended to the generic user_message by
# `raise_google_http_error`. Deliberately does NOT cover every status: a
# missing entry just means no extra hint is appended (the generic
# "no está disponible"/"no se pudo completar" message plus the numeric status
# already helps distinguish an outage from an auth/permissions problem).
# Review r2, finding P2-2: P3a's redaction correctly stripped `str(exc)`, but
# `include_exc_type=False` (mandatory — GoogleHttpError's class name IS
# "HttpError", the very substring the leak tests forbid) also flattened every
# HTTP status into one identical message, so a 403-insufficient-scope looked
# exactly like a 503 outage to the end user.
_GOOGLE_HTTP_STATUS_HINTS: dict[int, str] = {
    401: "Revisá los permisos otorgados a CashIn en tu cuenta de Google",
    403: "Revisá los permisos otorgados a CashIn en tu cuenta de Google",
    404: "El recurso solicitado no existe o fue eliminado",
    429: "Demasiadas solicitudes — reintentá en un momento",
}


def _google_http_status(exc: BaseException) -> int | None:
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        return None
    try:
        return int(status)
    except (TypeError, ValueError):
        return None


def raise_google_http_error(
    logger: structlog.BoundLogger,
    service: str,
    user_message: str,
    exc: BaseException,
    event: str,
    **log_context: object,
) -> NoReturn:
    """Log the raw `googleapiclient.errors.HttpError`, then raise
    `ExternalServiceError` with the response status code and a short,
    class-specific Spanish hint — instead of the single identical message
    every status used to collapse into (review r2, finding P2-2).

    Typed as `exc: BaseException` (not `GoogleHttpError`) deliberately: this
    module stays free of a direct `googleapiclient` import so it doesn't pick
    up mypy's "missing library stubs" noise for a package that has none —
    same reasoning `raise_external_service_error` already follows for every
    other exception type it accepts. The status is read defensively via
    `getattr`, matching `GOOGLE_TRANSPORT_ERRORS` handling's own style.

    Never includes `str(exc)` or the exception's class name ("HttpError"
    would reintroduce the "http" substring the leak tests forbid — same
    reasoning as `include_exc_type=False` on `raise_external_service_error`);
    only the numeric status and a canned hint string are safe to surface.
    """
    status = _google_http_status(exc)
    logger.warning(event, error=str(exc), exc_type=type(exc).__name__, status=status, **log_context)
    parts = [user_message]
    if status is not None:
        parts.append(f"(código {status})")
    hint = _GOOGLE_HTTP_STATUS_HINTS.get(status) if status is not None else None
    if hint:
        parts.append(f"— {hint}")
    raise ExternalServiceError(service, " ".join(parts)) from exc
