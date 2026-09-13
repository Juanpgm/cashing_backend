"""Tests for SECOP configuration diagnostics (Slice 1, tasks 1.9-1.13):

- Empty `SECOP_APP_TOKEN` never raises at startup; the warning is queryable.
- `secop_service.verificar_configuracion_secop()` reports token presence/health.
- `verificar_configuracion_secop` is registered as a read tool.
"""

from __future__ import annotations

import pytest
import structlog

# This pair used to be order-dependent on the FULL suite (passed in isolation,
# passed paired with one other file, failed in the full ~2450-test run) because
# `app/main.py` set `cache_logger_on_first_use=True` unconditionally: whichever
# test first triggered a real call on `app.core.config`'s module-level logger
# proxy cached that binding for the rest of the process, and this file's
# `capture_logs()` could silently miss it depending on suite order. Root cause
# fixed in `app/main.py` (cache disabled under pytest) rather than worked
# around here -- this file is back to the plain, idiomatic form specifically
# to prove that fix: if the root cause were still present, this would still be
# flaky in full-suite order.


class TestSecopTokenWarning:
    def test_empty_token_does_not_raise_and_logs_warning(self) -> None:
        from app.core.config import Settings

        with structlog.testing.capture_logs() as captured:
            s = Settings(SECOP_APP_TOKEN="")

        assert s.SECOP_APP_TOKEN == ""
        warn_events = [e for e in captured if e.get("event") == "secop_app_token_missing"]
        assert warn_events, "expected a queryable warning log when SECOP_APP_TOKEN is empty"

    def test_configured_token_does_not_warn(self) -> None:
        from app.core.config import Settings

        with structlog.testing.capture_logs() as captured:
            Settings(SECOP_APP_TOKEN="a-real-token-value")

        warn_events = [e for e in captured if e.get("event") == "secop_app_token_missing"]
        assert not warn_events


class TestVerificarConfiguracionSecop:
    @pytest.mark.asyncio
    async def test_reports_degraded_when_token_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import secop_service

        monkeypatch.setattr(secop_service.settings, "SECOP_APP_TOKEN", "")
        result = await secop_service.verificar_configuracion_secop()

        assert result.status == "degraded"
        assert result.token_configured is False
        assert result.warning

    @pytest.mark.asyncio
    async def test_reports_ok_when_token_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import secop_service

        monkeypatch.setattr(secop_service.settings, "SECOP_APP_TOKEN", "a-real-token-value")
        result = await secop_service.verificar_configuracion_secop()

        assert result.status == "ok"
        assert result.token_configured is True
        assert result.warning is None


class TestVerificarConfiguracionSecopToolRegistration:
    def test_tool_is_registered_as_read(self) -> None:
        import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
        from app.tools.registry import TOOL_REGISTRY

        assert "verificar_configuracion_secop" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["verificar_configuracion_secop"]
        assert spec.tags == ("read",)
