"""Tests for `radicacion_prep_service.preparar_radicacion` (billing-resilience-
templates, slice #7, tasks 7.6-7.11): orchestrates checklist verification,
coherence validation, and packaging IN ORDER. Any HARD coherence finding
halts BEFORE packaging (`COHERENCE_CHECK_FAILED`); the packager's own gates
(`SECRET_DETECTED_IN_PACKAGE`, `PACKAGE_PENDIENTE`) propagate unchanged.

Mirrors the fixture/mocking conventions of `tests/test_coherence_gate.py` and
`tests/test_paquete_gate.py`.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.core.exceptions import ValidationError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import informe_service, radicacion_prep_service
from app.tools.catalog.radicacion import PreparaRadicacionInput
from app.tools.catalog.requisitos import InferirRequisitosEstructuradosInput
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
        numero_contrato="CTR-RADICACION-PREP-001",
        objeto="Servicios profesionales para pruebas de preparar_radicacion",
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
    contrato.obligaciones = [ob]
    return ob


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


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.download = AsyncMock(return_value=b"contenido de evidencia de prueba")
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


@pytest.fixture
async def cuenta_lista(
    db: AsyncSession,
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato: Contrato,
    obligacion: Obligacion,
) -> CuentaCobro:
    """A cuenta whose checklist is fully complete and which has at least one
    evidencia on its only obligación (so the packager's LISTO/PENDIENTE split
    reports LISTO)."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    act = Actividad(
        cuenta_cobro_id=cuenta.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad realizada",
        justificacion="Justificación",
        fecha_realizacion=date(2024, 1, 10),
    )
    db.add(act)
    await db.commit()
    await db.refresh(act)

    ev = Evidencia(
        actividad_id=act.id,
        storage_key=f"evidencias/{act.id}/foto.jpg",
        nombre_archivo="foto.jpg",
        tipo_archivo="image/jpeg",
        tamano_bytes=12,
    )
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    act.evidencias = [ev]
    await db.refresh(cuenta)
    return cuenta


# ── 7.6-7.7 — full successful orchestration, in order ───────────────────────


async def test_preparar_radicacion_full_success_returns_package_location_and_listo(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _fake_storage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)

    resultado = await radicacion_prep_service.preparar_radicacion(db, test_user["user"].id, cuenta_lista.id)

    assert resultado.cuenta_cobro_id == cuenta_lista.id
    assert resultado.storage_key.startswith("paquetes/")
    assert resultado.filename.endswith(".zip")
    assert resultado.size_bytes > 0
    assert resultado.listo_para_radicar is True
    assert resultado.pendientes == 0
    assert resultado.advertencias_coherencia == []
    assert resultado.es_borrador is True
    storage.upload.assert_awaited_once()


# ── Checklist gate (runs FIRST) ──────────────────────────────────────────────


async def test_preparar_radicacion_raises_checklist_incomplete_when_pending(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    cuenta = await _make_cuenta(db, contrato, mes=1)

    with pytest.raises(ValidationError) as excinfo:
        await radicacion_prep_service.preparar_radicacion(db, test_user["user"].id, cuenta.id)
    assert excinfo.value.code == "CHECKLIST_INCOMPLETE"


# ── 7.8-7.9 — HARD coherence finding halts BEFORE packaging ─────────────────


async def test_preparar_radicacion_halts_before_packaging_on_hard_coherence_finding(
    db: AsyncSession,
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato: Contrato,
    obligacion: Obligacion,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied seguridad-social block (R2) between consecutive cuentas is a HARD
    finding — must raise COHERENCE_CHECK_FAILED and never reach the packager."""
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

    packager_spy = AsyncMock(side_effect=AssertionError("packager must not run when a HARD finding halts first"))
    monkeypatch.setattr(informe_service, "generar_zip_evidencias", packager_spy)

    with pytest.raises(ValidationError) as excinfo:
        await radicacion_prep_service.preparar_radicacion(db, test_user["user"].id, cuenta2.id)
    assert excinfo.value.code == "COHERENCE_CHECK_FAILED"
    packager_spy.assert_not_awaited()


# ── 7.10-7.11 — secret detection halts, propagated unchanged ────────────────


async def test_preparar_radicacion_propagates_secret_detected_in_package(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_secret(*_a: Any, **_k: Any) -> tuple[bytes, str]:
        raise ValidationError("secreto detectado", code="SECRET_DETECTED_IN_PACKAGE")

    monkeypatch.setattr(informe_service, "generar_zip_evidencias", _raise_secret)

    with pytest.raises(ValidationError) as excinfo:
        await radicacion_prep_service.preparar_radicacion(db, test_user["user"].id, cuenta_lista.id)
    assert excinfo.value.code == "SECRET_DETECTED_IN_PACKAGE"


async def test_preparar_radicacion_propagates_package_pendiente(
    db: AsyncSession,
    test_user: dict[str, Any],
    client: AsyncClient,
    contrato: Contrato,
    obligacion: Obligacion,
) -> None:
    """No evidencia uploaded for the only obligación -> the packager's own
    `modo="final"` gate raises PACKAGE_PENDIENTE; propagated unchanged."""
    cuenta = await _make_cuenta(db, contrato, mes=1)
    await _completar_checklist(client, test_user["headers"], cuenta.id)

    with pytest.raises(ValidationError) as excinfo:
        await radicacion_prep_service.preparar_radicacion(db, test_user["user"].id, cuenta.id)
    assert excinfo.value.code == "PACKAGE_PENDIENTE"


# ── 7.12 — tool registration ─────────────────────────────────────────────────


async def test_tools_registered_with_expected_tags() -> None:
    assert TOOL_REGISTRY["preparar_radicacion"].tags == ("write",)
    assert TOOL_REGISTRY["inferir_requisitos_estructurados"].tags == ("read",)


async def test_invoke_inferir_requisitos_estructurados(
    db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.adapters.llm as llm_pkg
    from app.schemas.agent import LLMResponse

    class _FakeLLM:
        async def complete(self, *a: Any, **k: Any) -> LLMResponse:
            return LLMResponse(
                content='{"requisitos": [{"codigo": "RUT", "etiqueta": "RUT", "categoria": "identificacion"}]}',
                model="fake",
                total_tokens=5,
            )

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(), raising=True)

    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool(
        "inferir_requisitos_estructurados", ctx, InferirRequisitosEstructuradosInput(texto="Aporte el RUT.")
    )

    assert len(result.requisitos) == 1
    assert result.requisitos[0].codigo == "RUT"
    assert result.requisitos[0].categoria == "identificacion"


async def test_invoke_preparar_radicacion(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_lista: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _fake_storage()
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.services.radicacion_prep_service._get_storage", lambda *_a, **_k: storage)

    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool("preparar_radicacion", ctx, PreparaRadicacionInput(cuenta_id=cuenta_lista.id))

    assert result.cuenta_cobro_id == cuenta_lista.id
    assert result.listo_para_radicar is True
    assert result.es_borrador is True
    assert result.storage_key.startswith("paquetes/")
