"""Tests for app.services.discovery_cache — standalone in-process TTL cache for
evidence-discovery results (radicar-ui-ux-improvements slice 5a / design §5).

Standalone: keyed by (usuario_id, cuenta_id, ventana_inicio, ventana_fin) — does NOT
touch stepper_state_service.py or the 7-predicate StepperStateResponse shape.
"""

from __future__ import annotations

import uuid

import pytest
from app.schemas.google_workspace import EvidenceDiscoveryResponse
from app.services import discovery_cache


def _response(resumen: str = "stub") -> EvidenceDiscoveryResponse:
    return EvidenceDiscoveryResponse(obligaciones=[], resumen=resumen, total_evidencias=0, fuentes={})


@pytest.fixture(autouse=True)
def _clear_cache():
    discovery_cache.clear()
    yield
    discovery_cache.clear()


def test_same_key_returns_cached_value_within_ttl():
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    value = _response("primera corrida")

    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", value)
    cached = discovery_cache.get_cached(usuario_id, cuenta_id, "2024-04-01", "2024-04-30")

    assert cached is value


def test_different_window_is_a_cache_miss():
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", _response())

    assert discovery_cache.get_cached(usuario_id, cuenta_id, "2024-05-01", "2024-05-31") is None


def test_different_usuario_id_never_leaks_another_tenants_cached_entry():
    """Tenant-boundary regression guard (design's explicit requirement): even for the
    same cuenta_id + ventana, a different usuario_id must never see the cached entry."""
    cuenta_id = uuid.uuid4()
    owner, other_user = uuid.uuid4(), uuid.uuid4()
    discovery_cache.store(owner, cuenta_id, "2024-04-01", "2024-04-30", _response("owner's result"))

    assert discovery_cache.get_cached(other_user, cuenta_id, "2024-04-01", "2024-04-30") is None


def test_entry_expires_after_ttl(monkeypatch: pytest.MonkeyPatch):
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(discovery_cache.settings, "DISCOVERY_CACHE_TTL_SECONDS", 0)
    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", _response())

    import time

    time.sleep(0.05)

    assert discovery_cache.get_cached(usuario_id, cuenta_id, "2024-04-01", "2024-04-30") is None
