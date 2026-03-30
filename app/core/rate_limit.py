"""Rate limiting configuration with slowapi."""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address, default_limits=["100/minute"])

# Specific rate limit decorators — use on endpoints:
# @limiter.limit("5/minute")   for auth endpoints
# @limiter.limit("20/minute")  for chat/LLM endpoints
# @limiter.limit("10/minute")  for upload endpoints
