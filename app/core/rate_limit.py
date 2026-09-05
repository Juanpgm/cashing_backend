"""Rate limiting configuration with slowapi."""

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])

# Specific rate limit decorators — use on endpoints:
# @limiter.limit("5/minute")   for auth endpoints
# @limiter.limit("20/minute")  for chat/LLM endpoints
# @limiter.limit("10/minute")  for upload endpoints


def upload_rate_limit_key(request: Request) -> str:
    """Rate-limit key for upload endpoints: the CALLER, not the reverse proxy.

    Railway terminates TLS in front of the container, so `get_remote_address`
    (which reads the socket peer) returns the same proxy IP for every user. The
    10/minute upload budget then becomes a single global budget: one contractor
    dropping ten files 429s everybody else on the platform.

    Resolution order:

    1. ``request.state.user_id`` — set by the auth dependency
       (``app.api.deps.get_current_user``) before the endpoint body runs, which is
       also when slowapi evaluates this function. This is the only key that is
       both stable and actually per-caller.
    2. The FIRST hop of ``X-Forwarded-For`` (``client, proxy1, proxy2``) for
       unauthenticated callers, so the limit still separates them once
       ``--proxy-headers`` is on.
    3. The socket peer, as before.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"

    forwarded = request.headers.get("x-forwarded-for", "")
    primer_salto = forwarded.split(",")[0].strip()
    if primer_salto:
        return primer_salto

    return get_remote_address(request)
