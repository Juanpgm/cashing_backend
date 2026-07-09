"""Tests for the two-tier document model + reset-on-delete.

- Cuenta-level documents (cuenta_cobro_id set) never leak into another cuenta.
- Contract-level documents (cuenta_cobro_id NULL) are shared: they auto-fulfil
  their requisito in every cuenta of the contract.
- ``listar_documentos_contrato`` returns only contract-level documents.
- Deleting a document resets any checklist row that linked it back to PENDIENTE.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.services import checklist_service, document_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"


async def _contrato(db: AsyncSession, user_id: Any) -> Contrato:
    c = Contrato(
        usuario_id=user_id,
        numero_contrato="CTR-SCOPE-001",
        objeto="Servicios para prueba de aislamiento por cuenta",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="MinTIC",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _cuenta(db: AsyncSession, contrato: Contrato, mes: int) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _fila(db: AsyncSession, cuenta: CuentaCobro, codigo: str) -> DocumentoCuentaCobro:
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta.id,
            DocumentoCuentaCobro.requisito_codigo == codigo,
        )
    )
    return res.scalar_one()


async def test_documento_no_cruza_entre_cuentas(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    c = await _contrato(db, user.id)
    ca = await _cuenta(db, c, mes=1)
    cb = await _cuenta(db, c, mes=2)

    # Document belongs strictly to cuenta A.
    df = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=c.id,
        cuenta_cobro_id=ca.id,
        storage_key="k/planilla.pdf",
        nombre="planilla-seguridad-social.pdf",
        tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
    )
    db.add(df)
    await db.commit()
    await db.refresh(df)

    await checklist_service.asegurar_checklist(db, ca)
    await checklist_service.asegurar_checklist(db, cb)
    await db.commit()

    # Auto-link on cuenta B must NOT pick up A's document.
    n_b = await checklist_service.auto_vincular_documentos_fuente(db, cb)
    await db.commit()
    assert n_b == 0
    assert (await _fila(db, cb, "SEGURIDAD_SOCIAL")).estado == EstadoRequisito.PENDIENTE

    # ...and it must not appear as a candidate in cuenta B either.
    payload_b = await checklist_service.construir_checklist_completo(db, cb)
    ss_b = next(i for i in payload_b["items"] if i["requisito"]["codigo"] == "SEGURIDAD_SOCIAL")
    assert all(d["id"] != df.id for d in ss_b["candidatos_documentos_fuente"])

    # But on its own cuenta A it links normally.
    n_a = await checklist_service.auto_vincular_documentos_fuente(db, ca)
    await db.commit()
    assert n_a >= 1
    assert (await _fila(db, ca, "SEGURIDAD_SOCIAL")).estado == EstadoRequisito.CARGADO


async def test_documento_nivel_contrato_se_comparte(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    c = await _contrato(db, user.id)
    ca = await _cuenta(db, c, mes=1)
    cb = await _cuenta(db, c, mes=2)

    # Contract-level RUT document: shared (cuenta_cobro_id NULL).
    df = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=c.id,
        cuenta_cobro_id=None,
        storage_key="k/rut.pdf",
        nombre="rut.pdf",
        tipo=TipoDocumentoFuente.RUT,
    )
    db.add(df)
    await db.commit()

    await checklist_service.asegurar_checklist(db, ca)
    await checklist_service.asegurar_checklist(db, cb)
    await db.commit()

    await checklist_service.auto_vincular_documentos_fuente(db, ca)
    await db.commit()
    await checklist_service.auto_vincular_documentos_fuente(db, cb)
    await db.commit()

    # The single shared RUT auto-fulfils the RUT requisito in BOTH cuentas.
    assert (await _fila(db, ca, "RUT")).estado == EstadoRequisito.CARGADO
    assert (await _fila(db, cb, "RUT")).estado == EstadoRequisito.CARGADO


async def test_listar_documentos_contrato_solo_nivel_contrato(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    c = await _contrato(db, user.id)
    ca = await _cuenta(db, c, mes=1)
    db.add_all(
        [
            DocumentoFuente(
                usuario_id=user.id,
                contrato_id=c.id,
                cuenta_cobro_id=None,
                storage_key="k/rut",
                nombre="rut.pdf",
                tipo=TipoDocumentoFuente.RUT,
            ),
            DocumentoFuente(
                usuario_id=user.id,
                contrato_id=c.id,
                cuenta_cobro_id=ca.id,
                storage_key="k/ss",
                nombre="planilla.pdf",
                tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
            ),
        ]
    )
    await db.commit()

    docs = await document_service.listar_documentos_contrato(db, user.id, c.id)
    nombres = {d.nombre for d in docs}
    assert "rut.pdf" in nombres  # contract-level → listed
    assert "planilla.pdf" not in nombres  # cuenta-level → excluded


async def test_crear_actividades_desde_obligaciones(db: AsyncSession, test_user: dict[str, Any]) -> None:
    from app.models.obligacion import Obligacion, TipoObligacion
    from app.services import cuenta_cobro_service, informe_service

    user = test_user["user"]
    c = await _contrato(db, user.id)
    db.add_all(
        [
            Obligacion(
                contrato_id=c.id, descripcion=f"Obligacion contractual numero {i}", tipo=TipoObligacion.GENERAL, orden=i
            )
            for i in range(2)
        ]
    )
    await db.commit()
    ca = await _cuenta(db, c, mes=1)

    resp = await cuenta_cobro_service.crear_actividades_desde_obligaciones(db, user.id, ca.id)
    await db.commit()
    assert resp.creadas == 2

    # Fresh read (production uses a separate request/session; the shared test session
    # would otherwise keep the stale empty actividades collection).
    db.expunge_all()
    # With actividades seeded, the informe now generates without error.
    content, filename = await informe_service.generar_informe_actividades_docx(db, user.id, ca.id)
    assert filename.endswith(".docx")
    assert len(content) > 1000


async def test_eliminar_documento_resetea_fila_a_pendiente(db: AsyncSession, test_user: dict[str, Any]) -> None:
    user = test_user["user"]
    c = await _contrato(db, user.id)
    ca = await _cuenta(db, c, mes=1)

    df = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=c.id,
        cuenta_cobro_id=ca.id,
        storage_key="k/rut.pdf",
        nombre="rut.pdf",
        tipo=TipoDocumentoFuente.RUT,
    )
    db.add(df)
    await db.commit()
    await db.refresh(df)

    await checklist_service.asegurar_checklist(db, ca)
    await checklist_service.vincular_documento_fuente(db, ca.id, "RUT", df.id)
    await db.commit()
    assert (await _fila(db, ca, "RUT")).estado == EstadoRequisito.CARGADO

    fake = AsyncMock()
    fake.delete = AsyncMock()
    with patch(_PATCH_S3, return_value=fake):
        await document_service.eliminar_documento(db, user.id, df.id)

    fila = await _fila(db, ca, "RUT")
    assert fila.estado == EstadoRequisito.PENDIENTE
    assert fila.documento_fuente_id is None


async def test_documento_nivel_contrato_multidoc_compartido(db: AsyncSession, test_user: dict[str, Any]) -> None:
    """RPC (contract-level) can hold MULTIPLE shared documents at once (e.g. RPC
    original + RPC de adición) in one cuenta, and the shared pool still
    auto-satisfies OTHER cuentas of the same contract (1:N model, two-tier)."""
    user = test_user["user"]
    c = await _contrato(db, user.id)
    ca = await _cuenta(db, c, mes=1)
    cb = await _cuenta(db, c, mes=2)

    df_original = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=c.id,
        cuenta_cobro_id=None,
        storage_key="k/rpc-original",
        nombre="rpc-original.pdf",
        tipo=TipoDocumentoFuente.RPC,
    )
    df_adicion = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=c.id,
        cuenta_cobro_id=None,
        storage_key="k/rpc-adicion",
        nombre="rpc-adicion.pdf",
        tipo=TipoDocumentoFuente.RPC,
    )
    db.add_all([df_original, df_adicion])
    await db.commit()
    await db.refresh(df_original)
    await db.refresh(df_adicion)

    await checklist_service.asegurar_checklist(db, ca)
    await checklist_service.asegurar_checklist(db, cb)
    await db.commit()

    # Cuenta A: explicitly link BOTH shared RPC documents to the same requisito.
    await checklist_service.vincular_documento_fuente(db, ca.id, "RPC", df_original.id)
    await checklist_service.vincular_documento_fuente(db, ca.id, "RPC", df_adicion.id)
    await db.commit()

    payload_a = await checklist_service.construir_checklist_completo(db, ca)
    rpc_a = next(i for i in payload_a["items"] if i["requisito"]["codigo"] == "RPC")
    assert rpc_a["estado"] == EstadoRequisito.CARGADO
    assert {d["id"] for d in rpc_a["documentos_fuente"]} == {df_original.id, df_adicion.id}

    # Cuenta B: never explicitly linked — auto-link from the SAME shared pool still
    # satisfies the requisito (conservative: picks a single best candidate).
    await checklist_service.auto_vincular_documentos_fuente(db, cb)
    await db.commit()
    payload_b = await checklist_service.construir_checklist_completo(db, cb)
    rpc_b = next(i for i in payload_b["items"] if i["requisito"]["codigo"] == "RPC")
    assert rpc_b["estado"] == EstadoRequisito.CARGADO
    assert len(rpc_b["documentos_fuente"]) >= 1
