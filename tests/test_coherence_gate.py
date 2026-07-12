"""Integration tests for the coherence gate in `radicar_cuenta` (billing-resilience-templates,
slice #1, tasks 1.18-1.21).

Covers: HARD finding blocks radicación with `COHERENCE_CHECK_FAILED`; SOFT-only findings
proceed; the `validar_coherencia_cuenta` tool runs read-only through `invoke_tool`; and
`COHERENCE_GATE_ENABLED=False` bypasses the gate entirely.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.core.config import settings
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.tools.catalog.coherence import ValidarCoherenciaInput
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_CODIGOS_OBLIGATORIOS = [
    "CONTRATO",
    "RPC",
    "SEGURIDAD_SOCIAL",
    "INFORME_ACTIVIDADES",
    "INFORME_SUPERVISION",
    "EVIDENCIAS",
    "CEDULA",
    "RUT",
    "ACTA_INICIO",
]


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-COHER-GATE-001",
        objeto="Servicios profesionales para pruebas del gate de coherencia",
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


async def _make_cuenta(db: AsyncSession, contrato: Contrato, mes: int, anio: int = 2024) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=mes,
        anio=anio,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


async def _completar_checklist(client: AsyncClient, headers: dict[str, str], cuenta_id: Any) -> None:
    r = await client.get(f"/api/v1/cuentas-cobro/{cuenta_id}/checklist", headers=headers)
    assert r.status_code == 200, r.text
    for codigo in _CODIGOS_OBLIGATORIOS:
        p = await client.patch(
            f"/api/v1/cuentas-cobro/{cuenta_id}/checklist/{codigo}",
            headers=headers,
            json={"cumplido_manual": True},
        )
        assert p.status_code == 200, p.text


async def test_validar_coherencia_cuenta_is_registered_as_read_tool() -> None:
    assert "validar_coherencia_cuenta" in TOOL_REGISTRY
    spec = TOOL_REGISTRY["validar_coherencia_cuenta"]
    assert spec.tags == ("read",)


async def test_invoke_tool_validar_coherencia_cuenta_returns_no_findings(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)
    ctx = ToolContext(db=db, usuario=test_user["user"])

    result = await invoke_tool("validar_coherencia_cuenta", ctx, ValidarCoherenciaInput(cuenta_id=cuenta.id))

    assert result.findings == []
    assert result.tiene_hard is False


async def test_radicar_blocked_by_hard_coherence_finding(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """A copied seguridad-social block (R2) between consecutive cuentas is a HARD
    finding — `radicar_cuenta` must raise `COHERENCE_CHECK_FAILED` and block."""
    cuenta1 = await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    texto = "Planilla 555666777 pagada por el contratista, sin novedad."
    for cuenta in (cuenta1, cuenta2):
        doc = DocumentoFuente(
            usuario_id=contrato.usuario_id,
            contrato_id=contrato.id,
            cuenta_cobro_id=cuenta.id,
            storage_key=f"fake/ss-{cuenta.mes}.pdf",
            nombre=f"ss-{cuenta.mes}.pdf",
            tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
            texto_extraido=texto,
        )
        db.add(doc)
    await db.commit()

    await _completar_checklist(client, test_user["headers"], cuenta2.id)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta2.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["code"] == "COHERENCE_CHECK_FAILED"


async def test_radicar_proceeds_with_soft_only_finding(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """A stale month name in a filename (R4, SOFT) never blocks radicación.

    Also covers task 3.12c: the response surfaces the SOFT finding in
    `advertencias_coherencia` so callers see it without a second
    `validar_coherencia_cuenta` call."""
    cuenta = await _make_cuenta(db, contrato, mes=3)
    doc = DocumentoFuente(
        usuario_id=contrato.usuario_id,
        contrato_id=contrato.id,
        cuenta_cobro_id=cuenta.id,
        storage_key="fake/informe-enero.docx",
        nombre="informe-enero-2024.docx",
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
    )
    db.add(doc)
    await db.commit()

    await _completar_checklist(client, test_user["headers"], cuenta.id)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["estado"] == "enviada"
    assert body["advertencias_coherencia"] is not None
    assert len(body["advertencias_coherencia"]) == 1
    assert body["advertencias_coherencia"][0]["rule_id"] == "R4"
    assert body["advertencias_coherencia"][0]["severity"] == "soft"


async def test_radicar_no_advertencias_when_all_rules_pass(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """Task 3.12c regression guard: `advertencias_coherencia` is absent/None when
    the coherence validator finds nothing to warn about."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["advertencias_coherencia"] is None


async def test_radicar_bypasses_gate_when_flag_disabled(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "COHERENCE_GATE_ENABLED", False)

    cuenta1 = await _make_cuenta(db, contrato, mes=1)
    cuenta2 = await _make_cuenta(db, contrato, mes=2)
    texto = "Planilla 555666777 pagada por el contratista, sin novedad."
    for cuenta in (cuenta1, cuenta2):
        doc = DocumentoFuente(
            usuario_id=contrato.usuario_id,
            contrato_id=contrato.id,
            cuenta_cobro_id=cuenta.id,
            storage_key=f"fake/ss-{cuenta.mes}.pdf",
            nombre=f"ss-{cuenta.mes}.pdf",
            tipo=TipoDocumentoFuente.SEGURIDAD_SOCIAL,
            texto_extraido=texto,
        )
        db.add(doc)
    await db.commit()

    await _completar_checklist(client, test_user["headers"], cuenta2.id)

    resp = await client.post(
        f"/api/v1/cuentas-cobro/{cuenta2.id}/radicar",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["estado"] == "enviada"
