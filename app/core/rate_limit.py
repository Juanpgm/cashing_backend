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
    """Rate-limit key for upload endpoints: the authenticated CALLER.

    Every upload endpoint requires auth, so `request.state.user_id` (set by
    `app.api.deps.get_current_user` before the endpoint body runs) is the only
    key that is both stable and actually per-caller. slowapi's `key_func` runs
    AFTER dependency resolution, so on these authenticated endpoints the
    anonymous branch below is never reachable in practice — it only guards
    against a misconfigured route that forgets the auth dependency.

    We deliberately do NOT read `X-Forwarded-For` here: it is a plain request
    header, fully controlled by whoever sends the HTTP request. A reverse
    proxy only APPENDS to it, so the LEFTMOST hop — the one this function would
    read — can be forged by any client, letting an attacker rotate their
    rate-limit bucket at will. The socket peer (`get_remote_address`) is the
    only fallback that isn't spoofable from outside the container.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"

    return get_remote_address(request)
