"""Tests for the top-level `scan_secop` aggregate status (slice 7a, C.2).

Context: `RequisitoChecklistItem.deteccion_error` already surfaces PER-REQUISITO
auto-SECOP/auto-link failures (see test_checklist_deteccion_errores.py). What was
missing is a TOP-LEVEL aggregate on `ChecklistResponse` so the frontend can show
one "auto-scan failed, retry" banner instead of the user having to notice
per-row states (usability-findings #9). `construir_checklist_completo()` derives
`scan_secop` from a per-request `db.info` marker set when `detectar_desde_secop`/
`auto_vincular_documentos_fuente` run, so `no_ejecutado` (never ran this request)
can be distinguished from `ok` (ran, zero errors) even though the errores map
itself stays empty in both cases.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.schemas.checklist import ChecklistResponse
from app.services import checklist_service
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-SCANSECOP-001",
        objeto="Servicios para prueba de scan_secop",
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


def test_checklist_response_without_scan_secop_still_validates() -> None:
    """Backward compatibility: an old-shaped payload (no `scan_secop` key) must
    still validate — additive, nullable field, old clients/payloads unaffected."""
    payload = {
        "cuenta_cobro_id": "11111111-1111-1111-1111-111111111111",
        "requisitos_definidos": True,
        "items": [],
        "resumen": {
            "total": 0,
            "cumplidos": 0,
            "pendientes": 0,
            "lista_pendientes": [],
            "radicacion_lista": False,
        },
        "arbol_evidencias": [],
    }
    resp = ChecklistResponse(**payload)
    assert resp.scan_secop is None


async def test_scan_secop_no_ejecutado_when_no_pass_ran(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """Standalone construir_checklist_completo call, no detectar_desde_secop/
    auto_vincular_documentos_fuente call beforehand → no_ejecutado."""
    cuenta = await _make_cuenta(db, contrato)

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()

    assert payload["scan_secop"]["estado"] == "no_ejecutado"


async def test_scan_secop_ok_after_clean_pass(db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]) -> None:
    """detectar_desde_secop ran with zero requisito-level errors → ok."""
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    await checklist_service.detectar_desde_secop(db, cuenta)
    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()

    assert payload["scan_secop"]["estado"] == "ok"


async def test_scan_secop_parcial_when_some_requisitos_error(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that completes but records per-requisito errors → parcial."""
    from app.models.secop import SecopDocumento

    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    db.add(
        SecopDocumento(
            id_documento_secop="DOC-SCANSECOP-CTR",
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

    assert payload["scan_secop"]["estado"] == "parcial"
    assert payload["scan_secop"]["requisitos_con_error"] >= 1


async def test_scan_secop_fallido_after_mocked_pass_level_exception(
    db: AsyncSession, contrato: Contrato, test_user: dict[str, Any]
) -> None:
    """The pass itself raising (not a per-requisito failure) is recorded via
    `marcar_scan_secop_fallido` (called from the endpoint's except-block wrapping
    the top-level refresh call, the previous swallow point) → fallido."""
    cuenta = await _make_cuenta(db, contrato)
    await checklist_service.asegurar_checklist(db, cuenta)

    checklist_service.marcar_scan_secop_fallido(db, cuenta.id, "No se pudo completar el escaneo automático de SECOP.")
    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()

    assert payload["scan_secop"]["estado"] == "fallido"
    assert payload["scan_secop"]["mensaje"] == "No se pudo completar el escaneo automático de SECOP."
