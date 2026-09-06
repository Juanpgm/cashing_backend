"""Resolve the real client IP behind Railway's reverse proxy.

Production runs on Railway behind its edge proxy, so the ASGI server's socket
peer (``scope["client"]``) is always the PROXY's address, never the caller's —
every IP-keyed rate limit collapses onto one platform-wide bucket and the audit
log records no useful client IPs.

Two conflicting hypotheses about Railway's edge behaviour exist (see the
Railway Help Station, 2026):

* A Railway employee states the edge "strips X-Forwarded-For ... and ensures
  clients cannot overwrite it; the first value is the real connecting IP", and
  that ``X-Real-IP`` is "a single source of truth for the connecting IP".
* Community replies claim the opposite: the edge APPENDS to
  ``X-Forwarded-For`` (so the last hop, not the first, is trustworthy) and its
  own hop lives in ``100.64.0.0/10`` (CGNAT).

`resolve_client_ip` is spoof-proof under BOTH hypotheses:

1. Prefer ``X-Real-IP`` when present and a valid IP address — under the
   "strip-and-set" hypothesis this is Railway's own authoritative value.
2. Otherwise walk ``X-Forwarded-For`` RIGHT TO LEFT and take the first PUBLIC
   address. Under "strip-and-set" there is only one entry and it is the real
   client IP. Under "append", the rightmost public entry is the one Railway's
   own edge appended (its CGNAT/private hop is skipped); the client-supplied
   LEFTMOST entry — fully attacker-controlled — is never read first. If every
   entry turns out private (e.g. an internal request), the rightmost valid
   entry is used as a best-effort fallback.
3. Otherwise fall back to the socket peer, unchanged.

This function never raises: malformed headers, empty values, thousands of
comma-separated entries, and headers longer than a few KB all degrade to the
next step in the resolution order instead of blowing up the request.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable, Mapping
from ipaddress import IPv4Address, IPv6Address
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send

# Carrier-Grade NAT range (RFC 6598) — used by some ISPs and cloud edges for
# internal hops. `ipaddress.is_private` does NOT cover this range, so it is
# checked explicitly.
_CGNAT_RANGE = ipaddress.ip_network("100.64.0.0/10")

# RFC1918 (IPv4) + ULA (IPv6) private-use ranges — checked explicitly instead
# of via `ip.is_private`. Verified against the stdlib: `is_private` ALSO
# flags RFC 5737 documentation/"TEST-NET" ranges (192.0.2.0/24,
# 198.51.100.0/24, 203.0.113.0/24) as private, because it implements the full
# IANA special-purpose registry, not just "not globally routable". Those
# TEST-NET addresses are the conventional stand-in for a real public client IP
# in tests/examples, so using the blanket `is_private` here would misclassify
# a genuine public-looking address as private. `is_loopback` and
# `is_link_local` do not have this problem and are used as-is below.
_PRIVATE_USE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_private_use(ip: IPv4Address | IPv6Address) -> bool:
    return any(ip.version == net.version and ip in net for net in _PRIVATE_USE_NETWORKS)

# Defensive caps against a hostile/misconfigured proxy: an X-Forwarded-For with
# thousands of entries or many KB of junk must not cost meaningful CPU per
# request. Only the tail is ever relevant (right-to-left resolution), so
# truncating from the left is safe.
_MAX_HEADER_CHARS = 4096
_MAX_XFF_ENTRIES = 32


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup — ASGI/HTTP header names are case-insensitive."""
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _parse_ip(raw: str) -> IPv4Address | IPv6Address | None:
    """Parse one address, tolerating brackets and a zone id. Never raises."""
    value = raw.strip()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end != -1:
            value = value[1:end]
    if "%" in value:
        value = value.split("%", 1)[0]
    if not value:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_public(ip: IPv4Address | IPv6Address) -> bool:
    is_cgnat = isinstance(ip, IPv4Address) and ip in _CGNAT_RANGE
    return not (ip.is_loopback or ip.is_link_local or _is_private_use(ip) or is_cgnat)


def resolve_client_ip(headers: Mapping[str, str], peer_host: str | None) -> str | None:
    """Resolve the real client IP from proxy headers, falling back to the peer.

    `headers` must support case-insensitive lookup by name (this function does
    the lower-casing itself; a plain ``dict`` with mixed-case keys works fine).
    """
    real_ip_raw = _get_header(headers, "x-real-ip")
    if real_ip_raw is not None:
        real_ip = _parse_ip(real_ip_raw)
        if real_ip is not None:
            return str(real_ip)

    xff_raw = _get_header(headers, "x-forwarded-for")
    if xff_raw is not None:
        # Bound the work done on a hostile header before splitting it.
        if len(xff_raw) > _MAX_HEADER_CHARS:
            xff_raw = xff_raw[-_MAX_HEADER_CHARS:]
        entries = [entry.strip() for entry in xff_raw.split(",")]
        entries = entries[-_MAX_XFF_ENTRIES:]
        parsed = [ip for raw in entries if (ip := _parse_ip(raw)) is not None]
        for ip in reversed(parsed):
            if _is_public(ip):
                return str(ip)
        if parsed:
            # Every entry was private/loopback/link-local/CGNAT — best-effort:
            # take the rightmost valid one rather than giving up entirely.
            return str(parsed[-1])

    return peer_host


def _decode_asgi_headers(raw_headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    """Decode ASGI's raw (bytes, bytes) header pairs.

    ASGI headers arrive as bytes; per the ASGI spec they must be decoded as
    latin-1 (not UTF-8) — latin-1 maps every byte 0-255 to a code point, so it
    can never raise on non-UTF-8 bytes, unlike a strict UTF-8 decode would.
    """
    # latin-1 is a total decode over every byte value 0-255, so this loop
    # cannot raise regardless of header content.
    return {key.decode("latin-1"): value.decode("latin-1") for key, value in raw_headers}


class TrustedProxyClientMiddleware:
    """Pure-ASGI middleware that replaces ``scope["client"]`` with the resolved IP.

    Deliberately NOT a ``BaseHTTPMiddleware`` subclass: that base class buffers
    the whole request/response through an in-memory stream, which breaks
    streaming uploads and Server-Sent Events. This middleware only inspects
    headers and the connection tuple, then forwards `receive`/`send` untouched.

    Applies to both `http` and `websocket` scopes. No-ops for `lifespan` and
    any other scope type, and no-ops entirely when
    ``settings.trust_proxy_headers_effective`` is False (local dev default).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket") or not settings.trust_proxy_headers_effective:
            await self.app(scope, receive, send)
            return

        headers = _decode_asgi_headers(scope.get("headers") or [])
        client = scope.get("client")
        peer_host = client[0] if client else None
        peer_port = client[1] if client and len(client) > 1 else 0

        resolved = resolve_client_ip(headers, peer_host)
        if resolved is not None:
            scope["client"] = (resolved, peer_port)

        await self.app(scope, receive, send)
