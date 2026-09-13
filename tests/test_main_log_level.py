"""Tests for the structlog log-level gate in `app.main` (Phase 0 observability slice).

`structlog.make_filtering_bound_logger(0)` (NOTSET) previously emitted
DEBUG-level logs unconditionally, including in production — noisy and a real
log-volume cost. Production must run at INFO; development keeps DEBUG.

`_wrapper_log_level()` is tested directly (not via re-running the module-level
`structlog.configure()`, which is process-global and would leak into every
other test in the suite).
"""

from __future__ import annotations

import logging

import app.main as main_module
import pytest
from app.core.config import settings


class TestLogLevelGate:
    def test_development_uses_debug_level(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ENVIRONMENT", "development")
        assert main_module._wrapper_log_level() == logging.DEBUG

    def test_production_uses_info_level_not_notset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "ENVIRONMENT", "production")
        level = main_module._wrapper_log_level()
        assert level == logging.INFO
        # NOTSET (0) would silently re-enable DEBUG-and-below in production.
        assert level != 0

    def test_unknown_environment_falls_back_to_info(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Anything that isn't literally "development" is treated as non-dev —
        same conservative default `settings.is_development` already uses."""
        monkeypatch.setattr(settings, "ENVIRONMENT", "staging")
        assert main_module._wrapper_log_level() == logging.INFO
