"""Tests for checklist auto-detection error isolation (hardening item #1).

Bug context: the SECOP auto-detection / auto-link path previously had no
per-requisito error isolation — a single failing requisito (e.g. a bad
document, a scoring exception) would blow up the whole scan, and nothing
told the caller which requisito failed. The fix:

  - Each requisito is processed inside its own try/except so one failure
    does not sink the rest of the scan.
  - The failure is logged with structured context (cuenta_id + requisito
    codigo).
  - The failing row gets a transient ``deteccion_error`` marker (set on the
    ORM instance for the lifetime of the request/session) that is surfaced
    in the API response via ``RequisitoChecklistItem.deteccion_error``,
    instead of the row silently staying PENDIENTE with no explanation.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
import structlog
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.secop import SecopDocumento
from app.services import checklist_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-DETERR-001",
        objeto="Servicios para prueba de errores de detección",
        valor_total=12_000_000,
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


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int = 1, anio: int = 2024) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
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


async def test_detectar_desde_secop_isolates_per_requisito_failures(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising detector for CONTRATO must not block RPC from being detected,
    and must log + surface the failure instead of silently staying PENDIENTE."""
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    # SECOP docs default categoria=OTROS (unmapped), so both go through the
    # keyword-scoring fallback branch, not the category-based primary path.
    db.add(
        SecopDocumento(
            id_documento_secop="DOC-CTR-ERR",
            numero_contrato=contrato.numero_contrato,
            nombre_archivo="Contrato firmado minuta clausulado.pdf",
            descripcion="Contrato",
            datos_raw={},
        )
    )
    db.add(
        SecopDocumento(
            id_documento_secop="DOC-RPC-OK",
            numero_contrato=contrato.numero_contrato,
            nombre_archivo="Registro Presupuestal RPC 123.pdf",
            descripcion="RPC",
            datos_raw={},
        )
    )
    await db.commit()

    original_keyword_score = checklist_service._keyword_score

    def _raise_for_contrato(textos: list[str | None], keywords: list[str]) -> Any:
        if "contrato" in keywords:
            raise RuntimeError("boom-detector")
        return original_keyword_score(textos, keywords)

    monkeypatch.setattr(checklist_service, "_keyword_score", _raise_for_contrato)

    with structlog.testing.capture_logs() as captured:
        resultado = await checklist_service.detectar_desde_secop(db, cuenta)
    await db.commit()

    # The raising requisito is present in the result (empty candidates) but
    # did not prevent RPC from being scored and auto-linked.
    assert resultado["CONTRATO"] == []
    assert resultado["RPC"]

    fila_rpc = await _fila(db, cuenta, "RPC")
    assert fila_rpc.estado == EstadoRequisito.DETECTADO
    assert fila_rpc.secop_documento_id is not None

    fila_ctr = await _fila(db, cuenta, "CONTRATO")
    assert fila_ctr.estado == EstadoRequisito.PENDIENTE
    assert fila_ctr.secop_documento_id is None
    assert checklist_service.obtener_deteccion_error(db, cuenta.id, "CONTRATO") is not None

    error_events = [e for e in captured if e.get("event") == "checklist_deteccion_error"]
    assert error_events, "expected a structured error log for the failing requisito"
    assert error_events[0]["cuenta_id"] == str(cuenta.id)
    assert error_events[0]["requisito_codigo"] == "CONTRATO"


async def test_construir_checklist_completo_surfaces_deteccion_error(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full checklist payload must expose deteccion_error per item so the
    API response (and eventually the UI) can show 'detección falló'."""
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    db.add(
        SecopDocumento(
            id_documento_secop="DOC-CTR-ERR-2",
            numero_contrato=contrato.numero_contrato,
            nombre_archivo="Contrato firmado minuta clausulado.pdf",
            descripcion="Contrato",
            datos_raw={},
        )
    )
    await db.commit()

    def _always_raise(textos: list[str | None], keywords: list[str]) -> Any:
        raise RuntimeError("boom-detector")

    monkeypatch.setattr(checklist_service, "_keyword_score", _always_raise)

    await checklist_service.detectar_desde_secop(db, cuenta)
    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()

    item_contrato = next(i for i in payload["items"] if i["requisito"]["codigo"] == "CONTRATO")
    assert item_contrato["deteccion_error"] is not None
    assert item_contrato["estado"] == EstadoRequisito.PENDIENTE


async def test_auto_vincular_documentos_fuente_isolates_per_requisito_failures(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising scorer for one requisito must not prevent auto-link of others."""
    from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente

    user = test_user["user"]
    cuenta = await _make_cuenta(db, contrato)
    # RUT is contract-level (shared doc) and RPC likewise — both auto-linkable by tipo.
    db.add(
        DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            storage_key="k/rut.pdf",
            nombre="rut.pdf",
            tipo=TipoDocumentoFuente.RUT,
        )
    )
    db.add(
        DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            storage_key="k/rpc.pdf",
            nombre="rpc.pdf",
            tipo=TipoDocumentoFuente.RPC,
        )
    )
    await db.commit()
    await checklist_service.asegurar_checklist(db, cuenta)

    original_score = checklist_service._score_fuente_para_requisito

    def _raise_for_rut(doc: Any, req_codigo: str) -> Any:
        if req_codigo == "RUT":
            raise RuntimeError("boom-scorer")
        return original_score(doc, req_codigo)

    monkeypatch.setattr(checklist_service, "_score_fuente_para_requisito", _raise_for_rut)

    with structlog.testing.capture_logs() as captured:
        vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    await db.commit()

    assert vinculados >= 1

    fila_rpc = await _fila(db, cuenta, "RPC")
    assert fila_rpc.estado == EstadoRequisito.CARGADO

    fila_rut = await _fila(db, cuenta, "RUT")
    assert fila_rut.estado == EstadoRequisito.PENDIENTE
    assert checklist_service.obtener_deteccion_error(db, cuenta.id, "RUT") is not None

    error_events = [e for e in captured if e.get("event") == "checklist_auto_vincular_error"]
    assert error_events
    assert error_events[0]["requisito_codigo"] == "RUT"
