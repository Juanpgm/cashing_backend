"""Standalone in-process TTL cache for evidence-discovery results.

radicar-ui-ux-improvements C.1 (design §5): consulted by
`evidence_discovery_service.descubrir_evidencias()` BEFORE the Gmail → Drive →
Calendar → matcher → justify chain runs, so a second call for the same
(usuario_id, cuenta_id, ventana) within TTL never re-hits Google. Deliberately
standalone — does NOT touch `stepper_state_service.py` or the 7-predicate
`StepperStateResponse` shape.

ponytail: plain in-process dict — correct for a single-worker FastAPI process, but
each worker in a future multi-worker/multi-instance deployment keeps its own cache
(a hit on worker A is still a miss on worker B — never a correctness bug, only a
lower hit rate). Upgrade path: swap the dict for Redis (already a `make up` Docker
Compose service) with the same key shape and `SETEX` for the TTL, if/when the app
moves to multiple workers.
"""

from __future__ import annotations

import time
import uuid

from app.core.config import settings
from app.schemas.google_workspace import EvidenceDiscoveryResponse

CacheKey = tuple[uuid.UUID, uuid.UUID, str, str]

_cache: dict[CacheKey, tuple[float, EvidenceDiscoveryResponse]] = {}


def _key(usuario_id: uuid.UUID, cuenta_id: uuid.UUID, fecha_inicio: str, fecha_fin: str) -> CacheKey:
    return (usuario_id, cuenta_id, fecha_inicio, fecha_fin)


def get_cached(
    usuario_id: uuid.UUID, cuenta_id: uuid.UUID, fecha_inicio: str, fecha_fin: str
) -> EvidenceDiscoveryResponse | None:
    """Returns the cached result for this exact key, or None on miss/expiry."""
    key = _key(usuario_id, cuenta_id, fecha_inicio, fecha_fin)
    entry = _cache.get(key)
    if entry is None:
        return None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        _cache.pop(key, None)
        return None
    return value


def store(
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    fecha_inicio: str,
    fecha_fin: str,
    value: EvidenceDiscoveryResponse,
) -> None:
    key = _key(usuario_id, cuenta_id, fecha_inicio, fecha_fin)
    _cache[key] = (time.monotonic() + settings.DISCOVERY_CACHE_TTL_SECONDS, value)


def clear() -> None:
    """Test-only helper: wipe the whole cache."""
    _cache.clear()
