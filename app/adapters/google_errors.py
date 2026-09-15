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

import httplib2
from google.auth.exceptions import RefreshError, TransportError

GOOGLE_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TimeoutError,
    httplib2.ServerNotFoundError,
    TransportError,
    RefreshError,
)
