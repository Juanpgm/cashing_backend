"""Standalone in-process TTL cache for `descubrir_evidencias` discovery results,
redeemed by `persistir_evidencias` (radicacion-sin-friccion 1.7).

Problem this solves: `persistir_evidencias` used to take the FULL
`list[ObligacionJustificada]` as a tool argument, forcing the calling LLM to
re-emit every discovered obligación/evidence-link verbatim (potentially dozens
of full JSON objects) just to trigger persistence of a result it already
produced one turn earlier. That burns prompt tokens and risks a re-typed
URL/date silently diverging from what discovery actually found.

Fix: `app.tools.catalog.evidencias.descubrir_evidencias` stashes its result
here under an opaque `handle_id` (returned to the LLM instead of the payload);
`persistir_evidencias` takes `cuenta_id` + that `handle_id` and redeems it
through `redeem()` below, which reuses the exact stored obligaciones list.

Storage choice: a plain in-process dict, the SAME pattern already established
by `app.services.discovery_cache` (evidence-discovery response cache) and
`app.core.secop_agentic_quota` (rate limiter) — both deliberately chose
in-process state over Redis (available via `docker-compose.infra.yml`, but
unused anywhere in this codebase for ephemeral server-side state) because a
single-worker FastAPI deployment makes it correct, not just convenient. Same
upgrade path noted there applies here: swap the dict for Redis `SETEX` with an
identical key shape if/when the app moves to multiple workers — a cache miss
on the "wrong" worker only degrades to "handle not found, re-run discovery"
(actionable, never a hang/crash), so Redis unavailability is not a concern this
module needs to guard against.

Redemption is intentionally NOT single-shot: `evidence_persist_service.
persistir_evidencias` is already idempotent (re-persisting the same
obligaciones list never duplicates Actividad/Evidencia rows — see its
docstring), so redeeming the same still-valid handle more than once is safe
and simply re-runs that idempotent persist. This is the safer default for an
LLM-driven caller that may retry `persistir_evidencias` after an ambiguous
network response: a single-use handle would force the caller to distinguish
"already persisted, this is fine" from a real error, which is exactly the kind
of judgment call an LLM is bad at. The tradeoff is a handle stays redeemable
until it expires rather than being revoked after first use — acceptable given
the TTL is short (`settings.EVIDENCE_HANDLE_TTL_SECONDS`) and redemption never
mutates data beyond what a single persist already would.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from app.core.config import settings
from app.core.exceptions import EvidenceHandleNotFoundError
from app.schemas.google_workspace import ObligacionJustificada


@dataclass(frozen=True)
class _HandleEntry:
    usuario_id: uuid.UUID
    cuenta_id: uuid.UUID | None
    obligaciones: list[ObligacionJustificada]


_cache: dict[str, tuple[float, _HandleEntry]] = {}


def store(
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID | None,
    obligaciones: list[ObligacionJustificada],
) -> str:
    """Stash `obligaciones` behind a fresh opaque handle and return it.

    `cuenta_id` may be `None` when `descubrir_evidencias` ran without one (e.g.
    free-form obligaciones + contrato_id only, no cuenta de cobro chosen yet):
    `redeem()` then accepts ANY `cuenta_id` for that handle, since there was
    nothing to scope it to at discovery time. Whenever `cuenta_id` IS provided
    here, `redeem()` enforces an exact match.
    """
    handle_id = str(uuid.uuid4())
    _cache[handle_id] = (
        time.monotonic() + settings.EVIDENCE_HANDLE_TTL_SECONDS,
        _HandleEntry(usuario_id=usuario_id, cuenta_id=cuenta_id, obligaciones=obligaciones),
    )
    return handle_id


def redeem(usuario_id: uuid.UUID, cuenta_id: uuid.UUID, handle_id: str) -> list[ObligacionJustificada]:
    """Return the obligaciones list stashed under `handle_id`, or raise.

    Raises `EvidenceHandleNotFoundError` — ONE outcome for every failure mode
    (unknown/malformed handle, expired handle, handle owned by another user,
    handle scoped to a different cuenta_id) — see that exception's docstring
    for why these are deliberately not distinguished.

    Does NOT delete the entry on success (see module docstring: redemption is
    intentionally repeatable within the TTL window).
    """
    entry = _cache.get(handle_id)
    if entry is None:
        raise EvidenceHandleNotFoundError()

    expires_at, value = entry
    if time.monotonic() >= expires_at:
        _cache.pop(handle_id, None)
        raise EvidenceHandleNotFoundError()

    if value.usuario_id != usuario_id:
        raise EvidenceHandleNotFoundError()

    if value.cuenta_id is not None and value.cuenta_id != cuenta_id:
        raise EvidenceHandleNotFoundError()

    return value.obligaciones


def clear() -> None:
    """Test-only helper: wipe the whole cache."""
    _cache.clear()
