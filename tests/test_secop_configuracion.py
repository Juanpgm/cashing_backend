"""Tests for SECOP configuration diagnostics (Slice 1, tasks 1.9-1.13):

- Empty `SECOP_APP_TOKEN` never raises at startup; the warning is queryable.
- `secop_service.verificar_configuracion_secop()` reports token presence/health.
- `verificar_configuracion_secop` is registered as a read tool.
"""

from __future__ import annotations

import pytest


# NOT `structlog.testing.capture_logs()`: that relies on the global processors
# list being mutated in place and every module's `structlog.get_logger(...)`
# proxy resolving against it -- true in isolation, but `app.core.config._log`
# is a MODULE-LEVEL proxy shared by every `Settings()` construction in the
# WHOLE suite (2000+ tests), some of which log through it before this test
# ever runs (structlog's `cache_logger_on_first_use=True`, app/main.py,
# caches the bound logger on whichever call happens first -- see
# gotcha/structlog-cache-logger-processors in project memory, 2 prior
# occurrences). `capture_logs()` worked for a single-session concurrency test
# (radicacion-sin-friccion 0.3) but is provably order-dependent across the
# full suite (radicacion-sin-friccion 0.4: passes in isolation or paired with
# one other file, fails in the full 2450-test run). Monkeypatching the
# module's own `_log` object sidesteps global structlog state entirely --
# this test no longer cares what any other test in the suite logged first.
class TestSecopTokenWarning:
    def test_empty_token_does_not_raise_and_logs_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.core.config as config_module

        captured: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            config_module._log,
            "warning",
            lambda event, **kw: captured.append((event, kw)),
        )

        s = config_module.Settings(SECOP_APP_TOKEN="")

        assert s.SECOP_APP_TOKEN == ""
        warn_events = [event for event, _ in captured if event == "secop_app_token_missing"]
        assert warn_events, "expected a queryable warning log when SECOP_APP_TOKEN is empty"

    def test_configured_token_does_not_warn(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.core.config as config_module

        captured: list[tuple[str, dict[str, object]]] = []
        monkeypatch.setattr(
            config_module._log,
            "warning",
            lambda event, **kw: captured.append((event, kw)),
        )

        config_module.Settings(SECOP_APP_TOKEN="a-real-token-value")

        warn_events = [event for event, _ in captured if event == "secop_app_token_missing"]
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
