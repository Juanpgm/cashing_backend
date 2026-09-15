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
- `google.auth.exceptions.RefreshError` — token refresh failure. Can happen
  LAZILY inside a call to `.execute()`, not just in `GmailAdapter.get_credentials`'s
  explicit `creds.refresh(...)` call, because the underlying transport may
  auto-refresh an expired token on demand.

Found in radicacion-sin-friccion phase 4.3 review r1 (finding P2b): a Railway
DNS blip or a lazy token-refresh failure previously surfaced as a raw 500,
never the 502 `ExternalServiceError` contract the frontend's error banner
relies on.
"""

from __future__ import annotations

from typing import NoReturn

import httplib2
import structlog
from google.auth.exceptions import RefreshError, TransportError

from app.core.exceptions import ExternalServiceError

GOOGLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    httplib2.ServerNotFoundError,
    TransportError,
    RefreshError,
)


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
    """
    logger.warning(event, error=str(exc), exc_type=type(exc).__name__, **log_context)
    detail = f"{user_message} ({type(exc).__name__})" if include_exc_type else user_message
    raise ExternalServiceError(service, detail) from exc
