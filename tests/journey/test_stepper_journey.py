"""Backend integration guarantees for the radicación stepper (radicacion-
stepper, work unit B6 — closes the backend delivery chain B1-B6).

This is NOT a red-green pair: `stepper-state` (B4) and `GET /paquete` (B5)
already have full unit/contract coverage. These are end-to-end assertions,
driven over the real HTTP API (`httpx.AsyncClient`, exactly like
`tests/journey/test_full_radicacion_journey.py`), that the two guarantees the
whole stepper design depends on actually hold when the endpoints are chained
together the way the frontend will call them:

  1. Read-only guarantee: `stepper-state` + `GET /paquete` in sequence never
     write to storage and never touch the credit balance.
  2. No-double-charge on resume: creating a cuota charges credits exactly
     once; re-entering the stepper via `stepper-state` (simulating a client
     that dropped all local state and reloaded `/radicar`) lands on the
     correct `current_step` and issues zero further
     `cuenta_cobro_service.crear_cuenta_cobro` calls, leaving the credit
     balance unchanged.

If either test fails, it is a real regression/gap in B1-B5 — per Strict TDD
instructions for this batch, the test must not be adjusted to paper over it.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.adapters.storage.port import StorageObjectInfo
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import cuenta_cobro_service
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    """Stateful in-memory `StoragePort` fake — same shape as the one in
    `tests/test_paquete_endpoints.py`, duplicated here (not imported) per the
    repo's existing convention of test files not importing fixtures/helpers
    from each other."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def upload(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._objects[key] = data
        return key

    async def download(self, key: str) -> bytes:
        return self._objects[key]

    async def presigned_url(self, key: str, expires_in: int = 3600) -> str:
        return f"fake://{key}"

    async def delete(self, key: str) -> None:
        self._objects.pop(key, None)

    async def list_objects(self, prefix: str) -> list[StorageObjectInfo]:
        return [StorageObjectInfo(key=k, size_bytes=len(v)) for k, v in self._objects.items() if k.startswith(prefix)]

    async def stat(self, key: str) -> StorageObjectInfo | None:
        data = self._objects.get(key)
        return StorageObjectInfo(key=key, size_bytes=len(data)) if data is not None else None


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-STEPPER-JOURNEY-001",
        objeto="Servicios profesionales para pruebas de integración del stepper",
        valor_total=24_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligacion(db: AsyncSession, contrato: Contrato) -> Obligacion:
    ob = Obligacion(
        contrato_id=contrato.id,
        descripcion="Obligación contractual de prueba con texto suficientemente largo",
        tipo=TipoObligacion.GENERAL,
        orden=0,
    )
    db.add(ob)
    await db.commit()
    await db.refresh(ob)
    # Identity-map staleness pattern documented in test_stepper_state.py /
    # test_paquete_endpoints.py: `contrato.obligaciones` was already eagerly
    # cached (empty) by the `contrato` fixture's own `db.refresh` before this
    # row existed — force a real reload.
    await db.refresh(contrato)
    return ob


async def _add_documento_contrato(
    db: AsyncSession,
    *,
    usuario_id: Any,
    contrato_id: Any,
    tipo: TipoDocumentoFuente,
    cuenta_cobro_id: Any | None = None,
) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=usuario_id,
        contrato_id=contrato_id,
        cuenta_cobro_id=cuenta_cobro_id,
        storage_key=f"documentos/{tipo.value}.pdf",
        nombre=f"{tipo.value}.pdf",
        tipo=tipo,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


async def _contrato_completo(db: AsyncSession, contrato: Contrato, usuario_id: Any) -> None:
    """Satisfy step 1's completeness predicate: a contract-level CONTRATO
    upload (narrowed rule — see `stepper_state_service._step1_contrato`'s
    docstring; RPC/CEDULA/RUT/ACTA_INICIO are no longer required here, only
    at the step-3 checklist gate)."""
    await _add_documento_contrato(
        db, usuario_id=usuario_id, contrato_id=contrato.id, tipo=TipoDocumentoFuente.CONTRATO
    )


# ── B6.1a — read-only guarantee: stepper-state + GET /paquete in sequence ──


async def test_stepper_state_then_get_paquete_produce_no_writes_and_no_credit_change(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chaining `GET /stepper-state` then `GET /paquete` — exactly how the
    stepper shell hydrates on every `/radicar` load — must not write to
    storage and must not touch the credit balance, regardless of how
    incomplete the cuota's checklist is."""
    user = test_user["user"]
    headers = test_user["headers"]
    saldo_antes = user.creditos_disponibles

    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)

    storage = _FakeStorage()
    upload_spy = AsyncMock(wraps=storage.upload)
    storage.upload = upload_spy  # type: ignore[method-assign]
    monkeypatch.setattr("app.services.paquete_service._get_storage", lambda *_a, **_k: storage)

    r1 = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/stepper-state", headers=headers)
    assert r1.status_code == 200, r1.text

    r2 = await client.get(f"/api/v1/cuentas-cobro/{cuenta.id}/paquete", headers=headers)
    assert r2.status_code == 200, r2.text
    assert r2.json()["existe"] is False

    upload_spy.assert_not_awaited()
    await db.refresh(user)
    assert user.creditos_disponibles == saldo_antes


# ── B6.1b — end-to-end resume: create -> drop -> re-enter, no re-charge ────


async def test_create_cuota_then_resume_lands_on_expected_step_without_recharging(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    obligacion: Obligacion,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full create -> drop client state -> re-enter resume flow:

    1. Contract-level mandatory docs are uploaded (step 1 becomes satisfiable).
    2. `POST /cuentas-cobro/` creates the cuota over the real HTTP API — the
       ONLY credit-charging call in this test — deducting
       `CREDITS_PER_CUENTA_COBRO` exactly once.
    3. The seguridad-social planilla is attached (step 2 becomes satisfiable).
    4. The client "drops all local state" (nothing to tear down server-side —
       readiness is derived purely from persisted DB state per the
       Resumability and Rehydration requirement) and re-enters `/radicar` by
       calling `GET /stepper-state` again, simulating a fresh page load.
    5. The resumed response must land on `current_step == 3` (steps 1-2
       complete, step 3's checklist mode not yet chosen — the contiguous-
       prefix rule), report `numero_cuota` non-null (the idempotency signal),
       and the re-entry must issue ZERO further
       `cuenta_cobro_service.crear_cuenta_cobro` calls, leaving the credit
       balance exactly at `saldo_inicial - CREDITS_PER_CUENTA_COBRO`.
    """
    user = test_user["user"]
    headers = test_user["headers"]
    saldo_inicial = user.creditos_disponibles

    await _contrato_completo(db, contrato, user.id)

    # ── Create the cuota via the real endpoint — the only charging call ─────
    r = await client.post(
        "/api/v1/cuentas-cobro/",
        headers=headers,
        json={"contrato_id": str(contrato.id), "mes": 1, "anio": 2024},
    )
    assert r.status_code == 201, r.text
    cuenta_body = r.json()
    cuenta_id = cuenta_body["id"]
    assert cuenta_body["numero_cuota"] == 1

    from app.core.config import settings

    await db.refresh(user)
    saldo_tras_creacion = saldo_inicial - settings.CREDITS_PER_CUENTA_COBRO
    assert user.creditos_disponibles == saldo_tras_creacion

    # ── Satisfy step 2 (SS planilla) so resume lands at step 3, not step 2 ──
    await _add_documento_contrato(
        db,
        usuario_id=user.id,
        contrato_id=contrato.id,
        tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
        cuenta_cobro_id=uuid.UUID(cuenta_id),
    )

    # ── "Drop client state" + re-enter: spy BEFORE the resumed call so any ──
    # accidental re-invocation during rehydration is caught.
    spy = AsyncMock(wraps=cuenta_cobro_service.crear_cuenta_cobro)
    monkeypatch.setattr(cuenta_cobro_service, "crear_cuenta_cobro", spy)

    r_resume = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/stepper-state", headers=headers)
    assert r_resume.status_code == 200, r_resume.text
    resumed = r_resume.json()

    assert resumed["numero_cuota"] == 1
    assert resumed["steps"][0]["complete"] is True  # step 1: contrato
    assert resumed["steps"][1]["complete"] is True  # step 2: cuota
    assert resumed["steps"][2]["complete"] is False  # step 3: checklist mode not yet chosen
    assert resumed["furthest_completed_step"] == 2
    assert resumed["current_step"] == 3

    spy.assert_not_called()
    await db.refresh(user)
    assert user.creditos_disponibles == saldo_tras_creacion

    # A second resume call (double rehydration, e.g. a re-mount) must also
    # stay a no-op on credits/creation.
    r_resume_again = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/stepper-state", headers=headers)
    assert r_resume_again.status_code == 200, r_resume_again.text
    spy.assert_not_called()
    await db.refresh(user)
    assert user.creditos_disponibles == saldo_tras_creacion
