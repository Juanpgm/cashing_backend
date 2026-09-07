"""Unit tests for `app/core/client_ip.py`.

Covers `resolve_client_ip` (the pure resolution function) against both
Railway edge hypotheses, and `TrustedProxyClientMiddleware` (the ASGI layer)
against http/websocket scopes, the on/off flag, and malformed input.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from app.core.client_ip import TrustedProxyClientMiddleware, resolve_client_ip
from app.core.config import settings


class TestResolveClientIpXRealIp:
    def test_valid_ipv4_is_used(self) -> None:
        assert resolve_client_ip({"X-Real-IP": "203.0.113.5"}, "10.1.1.1") == "203.0.113.5"

    def test_valid_ipv6_is_used(self) -> None:
        assert resolve_client_ip({"X-Real-IP": "2001:db8::1"}, "10.1.1.1") == "2001:db8::1"

    def test_ipv6_with_brackets_is_used(self) -> None:
        assert resolve_client_ip({"X-Real-IP": "[2001:db8::1]"}, "10.1.1.1") == "2001:db8::1"

    def test_ipv6_zone_id_is_stripped(self) -> None:
        assert resolve_client_ip({"X-Real-IP": "fe80::1%eth0"}, "10.1.1.1") == "fe80::1"

    def test_header_lookup_is_case_insensitive(self) -> None:
        assert resolve_client_ip({"x-real-ip": "203.0.113.5"}, "10.1.1.1") == "203.0.113.5"

    @pytest.mark.parametrize(
        "raw",
        ["not-an-ip", "", "1.2.3.4, 5.6.7.8", "   "],
    )
    def test_invalid_value_falls_through_to_xff(self, raw: str) -> None:
        headers = {"X-Real-IP": raw, "X-Forwarded-For": "198.51.100.1"}
        assert resolve_client_ip(headers, "10.1.1.1") == "198.51.100.1"

    def test_invalid_value_with_no_xff_falls_through_to_peer(self) -> None:
        assert resolve_client_ip({"X-Real-IP": "garbage"}, "10.1.1.1") == "10.1.1.1"


class TestResolveClientIpXForwardedFor:
    def test_single_public_entry_is_used(self) -> None:
        assert resolve_client_ip({"X-Forwarded-For": "203.0.113.9"}, "10.1.1.1") == "203.0.113.9"

    def test_forged_leftmost_is_ignored_rightmost_public_wins(self) -> None:
        """Under the 'append' hypothesis, the leftmost hop is client-controlled."""
        headers = {"X-Forwarded-For": "6.6.6.6, 203.0.113.77"}
        assert resolve_client_ip(headers, "10.1.1.1") == "203.0.113.77"

    def test_railway_internal_hop_appended_is_skipped(self) -> None:
        """A CGNAT (100.64.0.0/10) hop appended by Railway's own edge is not the client."""
        headers = {"X-Forwarded-For": "203.0.113.77, 100.64.1.1"}
        assert resolve_client_ip(headers, "10.1.1.1") == "203.0.113.77"

    def test_all_private_entries_returns_rightmost_valid(self) -> None:
        headers = {"X-Forwarded-For": "10.0.0.1, 172.16.0.1"}
        assert resolve_client_ip(headers, "10.1.1.1") == "172.16.0.1"

    def test_garbage_entries_are_skipped(self) -> None:
        headers = {"X-Forwarded-For": "foo, 203.0.113.9, ,"}
        assert resolve_client_ip(headers, "10.1.1.1") == "203.0.113.9"

    def test_thousand_entries_uses_only_last_32(self) -> None:
        # Build 1000 private entries, then a public one, then more private —
        # only the tail 32 should ever be examined; the public one at index
        # 500 is out of that window and must NOT be picked up.
        entries = ["10.0.0.1"] * 500 + ["203.0.113.55"] + ["172.16.0.1"] * 499
        headers = {"X-Forwarded-For": ", ".join(entries)}
        result = resolve_client_ip(headers, "10.1.1.1")
        # Only private entries remain in the last-32 window -> rightmost valid.
        assert result == "172.16.0.1"

    def test_thousand_entries_does_not_blow_up_and_picks_public_in_window(self) -> None:
        entries = ["10.0.0.1"] * 970 + ["203.0.113.55"] + ["172.16.0.1"] * 29
        headers = {"X-Forwarded-For": ", ".join(entries)}
        # 203.0.113.55 is now within the last-32 window and is public.
        assert resolve_client_ip(headers, "10.1.1.1") == "203.0.113.55"

    def test_whitespace_and_crlf_junk_is_tolerated(self) -> None:
        headers = {"X-Forwarded-For": " 203.0.113.9 ,\r\n10.0.0.1 "}
        assert resolve_client_ip(headers, "10.1.1.1") == "203.0.113.9"

    def test_empty_header_falls_back_to_peer(self) -> None:
        assert resolve_client_ip({"X-Forwarded-For": ""}, "10.1.1.1") == "10.1.1.1"

    def test_only_invalid_entries_falls_back_to_peer(self) -> None:
        assert resolve_client_ip({"X-Forwarded-For": "foo, bar, ,"}, "10.1.1.1") == "10.1.1.1"

    def test_header_longer_than_4kb_does_not_blow_up(self) -> None:
        junk = "10.0.0.1, " * 2000  # far more than 4KB
        headers = {"X-Forwarded-For": junk + "203.0.113.66"}
        result = resolve_client_ip(headers, "10.1.1.1")
        assert result == "203.0.113.66"


class TestResolveClientIpNoHeaders:
    def test_no_headers_returns_peer(self) -> None:
        assert resolve_client_ip({}, "203.0.113.9") == "203.0.113.9"

    def test_no_headers_and_no_peer_returns_none(self) -> None:
        assert resolve_client_ip({}, None) is None

    def test_headers_present_but_no_peer_still_resolves(self) -> None:
        assert resolve_client_ip({"X-Real-IP": "203.0.113.9"}, None) == "203.0.113.9"

    def test_nothing_at_all_returns_none(self) -> None:
        assert resolve_client_ip({}, None) is None


ASGIApp = Callable[[dict[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]]


async def _noop_receive() -> dict[str, Any]:  # pragma: no cover — never awaited by these tests
    return {"type": "http.disconnect"}


async def _noop_send(message: Any) -> None:  # pragma: no cover — no responses asserted here
    return None


async def _capture_scope_app(captured: dict[str, Any]) -> ASGIApp:
    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        captured["scope"] = scope

    return app


@pytest.fixture
def trust_proxy_headers_on() -> Any:
    previous = settings.TRUST_PROXY_HEADERS
    settings.TRUST_PROXY_HEADERS = True
    yield
    settings.TRUST_PROXY_HEADERS = previous


@pytest.fixture
def trust_proxy_headers_off() -> Any:
    previous = settings.TRUST_PROXY_HEADERS
    settings.TRUST_PROXY_HEADERS = False
    yield
    settings.TRUST_PROXY_HEADERS = previous


def _http_scope(headers: dict[str, str], client: tuple[str, int] | None) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": client,
    }


def _websocket_scope(headers: dict[str, str], client: tuple[str, int] | None) -> dict[str, Any]:
    scope = _http_scope(headers, client)
    scope["type"] = "websocket"
    return scope


@pytest.mark.asyncio
class TestTrustedProxyClientMiddleware:
    async def test_flag_off_leaves_peer_unchanged_even_with_headers(self, trust_proxy_headers_off: None) -> None:
        captured: dict[str, Any] = {}

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            captured["scope"] = scope

        middleware = TrustedProxyClientMiddleware(downstream)
        scope = _http_scope({"X-Forwarded-For": "203.0.113.9"}, ("10.0.0.5", 12345))

        await middleware(scope, _noop_receive, _noop_send)

        assert captured["scope"]["client"] == ("10.0.0.5", 12345)

    async def test_flag_on_replaces_client_with_resolved_ip(self, trust_proxy_headers_on: None) -> None:
        captured: dict[str, Any] = {}

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            captured["scope"] = scope

        middleware = TrustedProxyClientMiddleware(downstream)
        scope = _http_scope({"X-Forwarded-For": "203.0.113.9"}, ("10.0.0.5", 12345))

        await middleware(scope, _noop_receive, _noop_send)

        assert captured["scope"]["client"] == ("203.0.113.9", 12345)

    async def test_websocket_scope_is_resolved_too(self, trust_proxy_headers_on: None) -> None:
        captured: dict[str, Any] = {}

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            captured["scope"] = scope

        middleware = TrustedProxyClientMiddleware(downstream)
        scope = _websocket_scope({"X-Real-IP": "203.0.113.9"}, ("10.0.0.5", 12345))

        await middleware(scope, _noop_receive, _noop_send)

        assert captured["scope"]["client"] == ("203.0.113.9", 12345)
        assert captured["scope"]["type"] == "websocket"

    async def test_lifespan_scope_is_untouched(self, trust_proxy_headers_on: None) -> None:
        captured: dict[str, Any] = {}

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            captured["scope"] = scope

        middleware = TrustedProxyClientMiddleware(downstream)
        scope: dict[str, Any] = {"type": "lifespan"}

        await middleware(scope, _noop_receive, _noop_send)

        assert "client" not in captured["scope"]

    async def test_no_headers_no_peer_leaves_scope_untouched(self, trust_proxy_headers_on: None) -> None:
        captured: dict[str, Any] = {}

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            captured["scope"] = scope

        middleware = TrustedProxyClientMiddleware(downstream)
        scope = _http_scope({}, None)

        await middleware(scope, _noop_receive, _noop_send)

        assert captured["scope"]["client"] is None

    async def test_receive_and_send_are_forwarded_unmodified(self, trust_proxy_headers_on: None) -> None:
        """Streaming bodies must pass through untouched — no buffering."""
        received_messages = [{"type": "http.request", "body": b"chunk-1", "more_body": True}]
        sent_messages: list[Any] = []

        async def receive() -> dict[str, Any]:
            return received_messages.pop(0) if received_messages else {"type": "http.disconnect"}

        async def send(message: Any) -> None:
            sent_messages.append(message)

        async def downstream(scope: dict[str, Any], recv: Any, snd: Any) -> None:
            msg = await recv()
            await snd({"type": "http.response.start", "status": 200, "headers": []})
            await snd({"type": "http.response.body", "body": msg["body"]})

        middleware = TrustedProxyClientMiddleware(downstream)
        scope = _http_scope({"X-Forwarded-For": "203.0.113.9"}, ("10.0.0.5", 12345))

        await middleware(scope, receive, send)

        assert sent_messages[1]["body"] == b"chunk-1"

    async def test_malformed_header_bytes_do_not_raise(self, trust_proxy_headers_on: None) -> None:
        captured: dict[str, Any] = {}

        async def downstream(scope: dict[str, Any], receive: Any, send: Any) -> None:
            captured["scope"] = scope

        middleware = TrustedProxyClientMiddleware(downstream)
        scope: dict[str, Any] = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-forwarded-for", b"\xff\xfe not valid utf8 \x00\x01")],
            "client": ("10.0.0.5", 12345),
        }

        await middleware(scope, _noop_receive, _noop_send)

        # latin-1 decodes any byte sequence; the resulting garbage string
        # simply fails IP parsing and falls back to the peer.
        assert captured["scope"]["client"] == ("10.0.0.5", 12345)
