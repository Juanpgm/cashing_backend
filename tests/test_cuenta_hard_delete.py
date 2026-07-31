"""Hard-delete tests for `eliminar_cuenta_cobro` (cuenta-delete-and-reopen follow-up).

Soft-delete left hidden rows that still tripped the `uq_contrato_mes_anio` unique
index (Postgres enforces it on ALL rows, not just active ones), blocking the user
from recreating a cuota after deleting it. This module pins the real hard-delete
behavior: every row scoped to the cuenta is physically removed (not `deleted_at`),
in FK-safe explicit order, with best-effort storage cleanup after the DB commit.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.models.actividad import Actividad
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.clasificacion_job import ClasificacionEvidenciasJob
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, DocumentoRequisitoVinculo
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.usuario import Usuario
from app.schemas.cuenta_cobro import CuentaCobroCreate
from app.services import cuenta_cobro_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _make_user(db: AsyncSession) -> Usuario:
    user = Usuario(
        email="hard-delete@test.com",
        nombre="Test User",
        cedula="999999999",
        password_hash="hashed",
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato="HD-001-2024",
        objeto="Prestación de servicios para pruebas de hard delete",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Ministerio de Pruebas",
        dependencia="Dirección de Innovación",
        supervisor_nombre="Juan Supervisor",
    )
    db.add(contrato)
    await db.flush()
    return contrato


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.list_objects = AsyncMock(return_value=[])
    return storage


async def _seed_full_cuenta(db: AsyncSession, usuario: Usuario, contrato: Contrato) -> dict[str, Any]:
    """Build a CuentaCobro with one row in every child table `eliminar_cuenta_cobro`
    must touch, so the hard-delete test actually exercises every deletion branch."""
    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=6,
        anio=2024,
        valor=3_000_000,
        estado=EstadoCuentaCobro.ENVIADA,
        pdf_storage_key="pdfs/fake/cuenta.pdf",
    )
    db.add(cuenta)
    await db.flush()

    obligacion = Obligacion(contrato_id=contrato.id, descripcion="Obligación de prueba", tipo=TipoObligacion.GENERAL)
    db.add(obligacion)
    await db.flush()

    actividad = Actividad(cuenta_cobro_id=cuenta.id, descripcion="Actividad de prueba", obligacion_id=obligacion.id)
    db.add(actividad)
    await db.flush()

    evidencia = Evidencia(
        actividad_id=actividad.id,
        storage_key="evidencias/fake/evidencia.pdf",
        nombre_archivo="evidencia.pdf",
    )
    db.add(evidencia)
    await db.flush()

    enlace = EvidenciaObligacion(
        evidencia_id=evidencia.id, obligacion_id=obligacion.id, confianza="alta", status="confirmed"
    )
    db.add(enlace)

    job = ClasificacionEvidenciasJob(cuenta_cobro_id=cuenta.id, status="done", total=1, procesadas=1)
    db.add(job)

    borrador = BorradorCuentaCobro(cuenta_cobro_id=cuenta.id, version=1, contenido={"html": "<p>v1</p>"})
    db.add(borrador)

    requisito_cuenta = RequisitoCuenta(cuenta_cobro_id=cuenta.id, codigo="CUSTOM", etiqueta="Custom requisito")
    db.add(requisito_cuenta)
    await db.flush()

    documento_fuente = DocumentoFuente(
        usuario_id=usuario.id,
        cuenta_cobro_id=cuenta.id,
        storage_key="documentos/fake/doc.pdf",
        nombre="doc.pdf",
        tipo=TipoDocumentoFuente.RPC,
    )
    db.add(documento_fuente)
    await db.flush()

    documento_cuenta = DocumentoCuentaCobro(cuenta_cobro_id=cuenta.id, requisito_cuenta_id=requisito_cuenta.id)
    db.add(documento_cuenta)
    await db.flush()

    vinculo = DocumentoRequisitoVinculo(
        documento_cuenta_cobro_id=documento_cuenta.id, documento_fuente_id=documento_fuente.id
    )
    db.add(vinculo)

    conversacion = Conversacion(usuario_id=usuario.id, cuenta_cobro_id=cuenta.id, mensajes_json=[])
    db.add(conversacion)

    await db.commit()

    return {
        "cuenta": cuenta,
        "actividad": actividad,
        "evidencia": evidencia,
        "enlace": enlace,
        "job": job,
        "borrador": borrador,
        "requisito_cuenta": requisito_cuenta,
        "documento_cuenta": documento_cuenta,
        "vinculo": vinculo,
        "documento_fuente": documento_fuente,
        "conversacion": conversacion,
    }


async def test_eliminar_cuenta_cobro_hard_delete_borra_todo(db: AsyncSession) -> None:
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    seeded = await _seed_full_cuenta(db, user, contrato)
    cuenta_id = seeded["cuenta"].id

    storage = _mock_storage()
    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta_id, storage)
    await db.commit()

    # The cuenta and every row scoped to it must be GONE — not `deleted_at`-marked.
    assert (await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_id))).scalar_one_or_none() is None
    assert (
        await db.execute(select(Actividad).where(Actividad.id == seeded["actividad"].id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(Evidencia).where(Evidencia.id == seeded["evidencia"].id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(EvidenciaObligacion).where(EvidenciaObligacion.id == seeded["enlace"].id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.id == seeded["job"].id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(BorradorCuentaCobro).where(BorradorCuentaCobro.id == seeded["borrador"].id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(RequisitoCuenta).where(RequisitoCuenta.id == seeded["requisito_cuenta"].id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(DocumentoCuentaCobro).where(DocumentoCuentaCobro.id == seeded["documento_cuenta"].id))
    ).scalar_one_or_none() is None
    assert (
        await db.execute(select(DocumentoRequisitoVinculo).where(DocumentoRequisitoVinculo.id == seeded["vinculo"].id))
    ).scalar_one_or_none() is None

    # DocumentoFuente / Conversacion rows are NOT owned by the cuenta — they survive,
    # only the nullable FK pointing at the deleted cuenta is cleared.
    fuente = (
        await db.execute(select(DocumentoFuente).where(DocumentoFuente.id == seeded["documento_fuente"].id))
    ).scalar_one()
    assert fuente.cuenta_cobro_id is None
    conversacion = (
        await db.execute(select(Conversacion).where(Conversacion.id == seeded["conversacion"].id))
    ).scalar_one()
    assert conversacion.cuenta_cobro_id is None

    # Storage cleanup: evidencia storage_key + pdf_storage_key deleted, package
    # prefix listed (deterministic `paquetes/{usuario_id}/{cuenta_id}/`).
    storage.delete.assert_any_call("evidencias/fake/evidencia.pdf")
    storage.delete.assert_any_call("pdfs/fake/cuenta.pdf")
    storage.list_objects.assert_called_once_with(f"paquetes/{user.id}/{cuenta_id}/")


async def test_eliminar_cuenta_cobro_storage_failure_no_bloquea_el_delete(db: AsyncSession) -> None:
    """REL follow-up: storage cleanup is best-effort — a raising `storage.delete`
    must not stop the DB deletion from completing (and must not raise out)."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)
    seeded = await _seed_full_cuenta(db, user, contrato)
    cuenta_id = seeded["cuenta"].id

    storage = _mock_storage()
    storage.delete = AsyncMock(side_effect=Exception("storage is down"))

    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, cuenta_id, storage)
    await db.commit()

    assert (await db.execute(select(CuentaCobro).where(CuentaCobro.id == cuenta_id))).scalar_one_or_none() is None


async def test_eliminar_cuenta_cobro_libera_mes_anio_para_recrear(db: AsyncSession) -> None:
    """THE conflict the user hit: soft-delete left a tombstone row that still
    satisfied `uq_contrato_mes_anio` at the DB level (the constraint has no
    `WHERE deleted_at IS NULL` predicate), so recreating a cuota for the same
    contrato/mes/anio after "deleting" the old one used to fail. Hard-delete
    physically removes the row, so recreation must now succeed."""
    user = await _make_user(db)
    contrato = await _make_contrato(db, user.id)

    data = CuentaCobroCreate(contrato_id=contrato.id, mes=9, anio=2024, valor=Decimal("1000000.00"))
    original = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)
    await db.commit()

    # Give it a child row too — the exact case that would leave FK-orphaned data
    # (or block the delete under real FK enforcement) with a naive/soft delete.
    actividad = Actividad(cuenta_cobro_id=original.id, descripcion="Actividad antes de eliminar")
    db.add(actividad)
    await db.commit()

    await cuenta_cobro_service.eliminar_cuenta_cobro(db, user.id, original.id, _mock_storage())
    await db.commit()

    recreated = await cuenta_cobro_service.crear_cuenta_cobro(db, user.id, data)
    assert recreated.mes == 9
    assert recreated.anio == 2024
    assert recreated.id != original.id
