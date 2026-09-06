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

    We deliberately do NOT read `X-Forwarded-For` here ourselves: reading it
    naively (e.g. the leftmost hop) would let an attacker forge it and rotate
    their rate-limit bucket at will. `get_remote_address` below reads
    `request.client.host`, which `app/core/client_ip.py`'s
    `TrustedProxyClientMiddleware` already resolves safely (X-Real-IP, else
    X-Forwarded-For walked right-to-left for the first public address) when
    `TRUST_PROXY_HEADERS` is on — so this fallback gets the real caller IP in
    production without parsing headers a second time here.
    """
    user_id = getattr(request.state, "user_id", None)
    if user_id:
        return f"user:{user_id}"

    return get_remote_address(request)
