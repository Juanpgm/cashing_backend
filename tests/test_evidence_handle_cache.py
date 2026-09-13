"""Tests for app.services.evidence_handle_cache — standalone in-process TTL
cache that lets `persistir_evidencias` redeem a `descubrir_evidencias` result
by opaque handle instead of the LLM re-emitting the full obligaciones/evidencias
payload (radicacion-sin-friccion 1.7).
"""

from __future__ import annotations

import time
import unittest.mock
import uuid

import pytest
from app.core.exceptions import EvidenceHandleNotFoundError
from app.schemas.google_workspace import EvidenceLink, ObligacionJustificada
from app.services import evidence_handle_cache


def _obligaciones(n: int = 1, links_per: int = 1) -> list[ObligacionJustificada]:
    return [
        ObligacionJustificada(
            obligacion_id=str(uuid.uuid4()),
            descripcion=f"Obligación {i}",
            actividad=f"Actividad {i}",
            justificacion=f"Justificación {i}",
            origen="llm",
            evidencias=[
                EvidenceLink(source="email", titulo=f"Evidencia {i}-{j}", link=f"https://mail.example.com/{i}/{j}")
                for j in range(links_per)
            ],
        )
        for i in range(n)
    ]


@pytest.fixture(autouse=True)
def _clear_cache():
    evidence_handle_cache.clear()
    yield
    evidence_handle_cache.clear()


def test_store_then_redeem_returns_the_exact_stored_obligaciones():
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    obligaciones = _obligaciones(2)

    handle_id = evidence_handle_cache.store(usuario_id, cuenta_id, obligaciones)
    redeemed = evidence_handle_cache.redeem(usuario_id, cuenta_id, handle_id)

    assert redeemed == obligaciones


def test_ten_obligaciones_forty_links_round_trip_without_truncation():
    """The exact scenario the handle exists to fix: no data loss versus the old
    full-payload path, however large the discovery result was."""
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    obligaciones = _obligaciones(10, links_per=4)
    assert sum(len(ob.evidencias) for ob in obligaciones) == 40

    handle_id = evidence_handle_cache.store(usuario_id, cuenta_id, obligaciones)
    redeemed = evidence_handle_cache.redeem(usuario_id, cuenta_id, handle_id)

    assert len(redeemed) == 10
    assert sum(len(ob.evidencias) for ob in redeemed) == 40
    assert redeemed == obligaciones


def test_handle_scoped_to_none_cuenta_id_at_discovery_accepts_any_cuenta_at_persist():
    """`descubrir_evidencias` can run without a cuenta_id (free-form obligaciones
    + contrato_id only) — that handle must still be redeemable once the caller
    knows which cuenta_id to persist against."""
    usuario_id = uuid.uuid4()
    obligaciones = _obligaciones(1)

    handle_id = evidence_handle_cache.store(usuario_id, None, obligaciones)
    redeemed = evidence_handle_cache.redeem(usuario_id, uuid.uuid4(), handle_id)

    assert redeemed == obligaciones


def test_unknown_handle_raises_actionable_not_found():
    with pytest.raises(EvidenceHandleNotFoundError) as exc_info:
        evidence_handle_cache.redeem(uuid.uuid4(), uuid.uuid4(), "does-not-exist")

    assert "descubrir_evidencias" in exc_info.value.detail


def test_malformed_handle_string_raises_cleanly_not_a_crash():
    """A handle that isn't even UUID-shaped must reject cleanly, same as any
    other unknown handle — never an unhandled exception."""
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    evidence_handle_cache.store(usuario_id, cuenta_id, _obligaciones(1))

    with pytest.raises(EvidenceHandleNotFoundError):
        evidence_handle_cache.redeem(usuario_id, cuenta_id, "'; DROP TABLE evidencias; --")


def test_handle_belonging_to_different_user_is_rejected():
    owner, attacker = uuid.uuid4(), uuid.uuid4()
    cuenta_id = uuid.uuid4()
    handle_id = evidence_handle_cache.store(owner, cuenta_id, _obligaciones(1))

    with pytest.raises(EvidenceHandleNotFoundError):
        evidence_handle_cache.redeem(attacker, cuenta_id, handle_id)


def test_handle_for_different_cuenta_id_is_rejected_not_silently_persisted():
    usuario_id = uuid.uuid4()
    cuenta_a, cuenta_b = uuid.uuid4(), uuid.uuid4()
    handle_id = evidence_handle_cache.store(usuario_id, cuenta_a, _obligaciones(1))

    with pytest.raises(EvidenceHandleNotFoundError):
        evidence_handle_cache.redeem(usuario_id, cuenta_b, handle_id)


def test_expired_handle_raises_actionable_not_found(monkeypatch: pytest.MonkeyPatch):
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(evidence_handle_cache.settings, "EVIDENCE_HANDLE_TTL_SECONDS", 0)
    handle_id = evidence_handle_cache.store(usuario_id, cuenta_id, _obligaciones(1))

    time.sleep(0.05)

    with pytest.raises(EvidenceHandleNotFoundError) as exc_info:
        evidence_handle_cache.redeem(usuario_id, cuenta_id, handle_id)

    assert "descubrir_evidencias" in exc_info.value.detail


def test_expired_handle_is_popped_from_the_module_cache():
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    handle_id = evidence_handle_cache.store(usuario_id, cuenta_id, _obligaciones(1))
    assert handle_id in evidence_handle_cache._cache

    entry = evidence_handle_cache._cache[handle_id]
    evidence_handle_cache._cache[handle_id] = (time.monotonic() - 1, entry[1])

    with pytest.raises(EvidenceHandleNotFoundError):
        evidence_handle_cache.redeem(usuario_id, cuenta_id, handle_id)
    assert handle_id not in evidence_handle_cache._cache


def test_ttl_boundary_is_inclusive_of_expires_at():
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    handle_id = evidence_handle_cache.store(usuario_id, cuenta_id, _obligaciones(1))
    expires_at, _value = evidence_handle_cache._cache[handle_id]

    with (
        unittest.mock.patch.object(evidence_handle_cache.time, "monotonic", return_value=expires_at),
        pytest.raises(EvidenceHandleNotFoundError),
    ):
        evidence_handle_cache.redeem(usuario_id, cuenta_id, handle_id)


def test_double_redeem_of_same_still_valid_handle_succeeds_both_times():
    """Documents the STEP 2.3 decision: redemption is NOT single-shot. Safe for
    an LLM caller retrying `persistir_evidencias` after an ambiguous network
    response — the underlying persist is already idempotent, so a second
    redemption before expiry must succeed and return the SAME obligaciones,
    not a generic 'already consumed' rejection."""
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    obligaciones = _obligaciones(2)
    handle_id = evidence_handle_cache.store(usuario_id, cuenta_id, obligaciones)

    first = evidence_handle_cache.redeem(usuario_id, cuenta_id, handle_id)
    second = evidence_handle_cache.redeem(usuario_id, cuenta_id, handle_id)

    assert first == obligaciones
    assert second == obligaciones


def test_store_generates_a_fresh_handle_each_call():
    usuario_id, cuenta_id = uuid.uuid4(), uuid.uuid4()
    first = evidence_handle_cache.store(usuario_id, cuenta_id, _obligaciones(1))
    second = evidence_handle_cache.store(usuario_id, cuenta_id, _obligaciones(1))

    assert first != second
