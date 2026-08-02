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


def test_expired_entry_is_popped_from_the_module_cache():
    """A miss due to expiry must also evict the stale entry from `_cache` — not
    just return None while leaking the dead row forever (unbounded growth)."""
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", _response())
    key = discovery_cache._key(usuario_id, cuenta_id, "2024-04-01", "2024-04-30")
    assert key in discovery_cache._cache

    import time

    entry = discovery_cache._cache[key]
    discovery_cache._cache[key] = (time.monotonic() - 1, entry[1])  # force already-expired

    assert discovery_cache.get_cached(usuario_id, cuenta_id, "2024-04-01", "2024-04-30") is None
    assert key not in discovery_cache._cache


def test_ttl_boundary_is_inclusive_of_expires_at():
    """`>=` boundary: a lookup at the exact `expires_at` instant is already a miss."""
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", _response())
    key = discovery_cache._key(usuario_id, cuenta_id, "2024-04-01", "2024-04-30")
    expires_at, _value = discovery_cache._cache[key]

    import unittest.mock

    with unittest.mock.patch.object(discovery_cache.time, "monotonic", return_value=expires_at):
        assert discovery_cache.get_cached(usuario_id, cuenta_id, "2024-04-01", "2024-04-30") is None


def test_store_overwrite_refreshes_ttl():
    """Storing again for the same key must reset the TTL window, not keep the
    original (soon-to-expire) `expires_at`."""
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", _response("first"))
    key = discovery_cache._key(usuario_id, cuenta_id, "2024-04-01", "2024-04-30")
    first_expires_at, _ = discovery_cache._cache[key]

    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", _response("second"))
    second_expires_at, second_value = discovery_cache._cache[key]

    assert second_expires_at >= first_expires_at
    assert second_value.resumen == "second"


def test_cache_key_is_blind_to_obligaciones_content():
    """DOCUMENTED INVARIANT: the cache key is (usuario_id, cuenta_id, ventana) only
    — it deliberately does NOT include `obligaciones`. Two lookups for the same
    usuario/cuenta/window but a conceptually different obligaciones set still hit
    the same cached entry (the first one stored). Do NOT add obligaciones to the
    key — that would defeat the point of the cache (obligaciones is exactly the
    kind of per-call detail this cache exists to avoid recomputing)."""
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    first = _response("first result, obligaciones=[]")
    discovery_cache.store(usuario_id, cuenta_id, "2024-04-01", "2024-04-30", first)

    # A second "logically different" call (different obligaciones content, same
    # usuario/cuenta/window) is irrelevant to the key — it's still a hit on `first`.
    cached = discovery_cache.get_cached(usuario_id, cuenta_id, "2024-04-01", "2024-04-30")

    assert cached is first
