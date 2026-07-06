"""Tests for the per-user sliding-window agentic quota."""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.exceptions import RateLimitExceededError
from app.core.secop_agentic_quota import _reset, enforce_agentic_quota, remaining


@pytest.fixture(autouse=True)
def _clean() -> None:
    _reset()


def test_quota_allows_up_to_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECOP_AGENTIC_HOURLY_LIMIT", 3, raising=False)
    for _ in range(3):
        enforce_agentic_quota("user-a")
    assert remaining("user-a") == 0


def test_quota_blocks_after_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECOP_AGENTIC_HOURLY_LIMIT", 2, raising=False)
    enforce_agentic_quota("user-b")
    enforce_agentic_quota("user-b")
    with pytest.raises(RateLimitExceededError):
        enforce_agentic_quota("user-b")


def test_quota_is_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SECOP_AGENTIC_HOURLY_LIMIT", 1, raising=False)
    enforce_agentic_quota("user-c")
    # Different user is unaffected
    enforce_agentic_quota("user-d")
    assert remaining("user-c") == 0
    assert remaining("user-d") == 0
