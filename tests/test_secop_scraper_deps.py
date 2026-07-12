"""Tests for the SECOP II scraper adapter selection in the API DI layer.

Slice 3, task 3.2 / design D-adapter-selection rule. Distinct from
`tests/test_secop_scraper_adapter.py::TestFactory`, which tests the unflagged,
URL/token-only factory in `app.adapters.secop_scraper.get_secop_scraper`. This
module additionally gates on `SECOP_SCRAPER_ENABLED` — the flag must default
the manual scraper trigger to fully inert, even when URL/token happen to be
configured in an environment.
"""

from __future__ import annotations

import pytest
from app.adapters.secop_scraper import NullSecopScraperAdapter, SecopScraperHttpAdapter
from app.api.deps import get_secop_scraper
from app.core.config import settings


class TestGetSecopScraperGated:
    def test_flag_off_returns_null_even_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SECOP_SCRAPER_ENABLED", False, raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_URL", "http://scraper.test", raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_INTERNAL_TOKEN", "tok", raising=False)

        adapter = get_secop_scraper()

        assert isinstance(adapter, NullSecopScraperAdapter)

    def test_flag_on_and_configured_returns_http_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SECOP_SCRAPER_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_URL", "http://scraper.test", raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_INTERNAL_TOKEN", "tok", raising=False)

        adapter = get_secop_scraper()

        assert isinstance(adapter, SecopScraperHttpAdapter)

    def test_flag_on_but_unconfigured_returns_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SECOP_SCRAPER_ENABLED", True, raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_URL", "", raising=False)
        monkeypatch.setattr(settings, "SECOP_SCRAPER_INTERNAL_TOKEN", "", raising=False)

        adapter = get_secop_scraper()

        assert isinstance(adapter, NullSecopScraperAdapter)
