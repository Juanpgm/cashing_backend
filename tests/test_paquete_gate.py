"""Integration tests for the packager tools (billing-resilience-templates, slice #2,
tasks 2.16-2.17).

Mirrors `tests/test_coherence_gate.py`'s structure: tool registration, `invoke_tool`
read/write behavior, and `SECRET_SCAN_GATE_ENABLED=False` bypass.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.core.config import settings
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import informe_service
from app.tools.catalog.paquete import GenerarPaqueteEvidenciasInput, ObtenerEstadoListoPendienteInput
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_LEAK_CORPUS_TEXTO = (
    "Notas internas\n"
    "DATABASE_URL=postgresql://fake_user:fake_pass_123@ep-fake-000000"
    ".us-east-1.aws.neon.tech/neondb\n"
    "NEON_API_KEY=napi_a8K3mXpQ9rZtL2wVbN7cJhF4sYdE1oIu\n"
)


def _fake_storage(download_map: dict[str, bytes] | None = None) -> AsyncMock:
    download_map = download_map or {}
    storage = AsyncMock()

    async def _download(key: str) -> bytes:
        return download_map.get(key, b"contenido de evidencia de prueba")

    storage.download = AsyncMock(side_effect=_download)
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PAQUETE-GATE-001",
        objeto="Servicios profesionales para pruebas del gate del paquete",
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
    # `Contrato.obligaciones` is `lazy="selectin"` — the earlier `db.refresh(contrato)`
    # in the `contrato` fixture already cached it as loaded-empty in the session
    # identity map. Attach manually so later same-session loads see it (same
    # workaround `tests/test_informe_service.py` uses for its `obligaciones` fixture).
    contrato.obligaciones = [ob]
    return ob


@pytest.fixture
async def cuenta_con_evidencia(db: AsyncSession, contrato: Contrato, obligacion: Obligacion) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=5,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)

    act = Actividad(
        cuenta_cobro_id=cc.id,
        obligacion_id=obligacion.id,
        descripcion="Actividad realizada",
        justificacion="Justificación",
        fecha_realizacion=date(2024, 5, 10),
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
    # `Actividad.evidencias` is `lazy="selectin"` too — same identity-map staleness
    # as `Contrato.obligaciones` above; attach manually.
    act.evidencias = [ev]
    await db.refresh(cc)
    return cc


async def test_tools_registered_with_expected_tags() -> None:
    assert TOOL_REGISTRY["generar_paquete_evidencias"].tags == ("write",)
    assert TOOL_REGISTRY["obtener_estado_listo_pendiente"].tags == ("read",)


async def test_invoke_obtener_estado_listo_pendiente(
    db: AsyncSession, test_user: dict[str, Any], cuenta_con_evidencia: CuentaCobro
) -> None:
    ctx = ToolContext(db=db, usuario=test_user["user"])

    result = await invoke_tool(
        "obtener_estado_listo_pendiente", ctx, ObtenerEstadoListoPendienteInput(cuenta_id=cuenta_con_evidencia.id)
    )

    assert result.cuenta_cobro_id == cuenta_con_evidencia.id
    assert result.pendientes == 0
    assert result.listo_para_radicar is True
    assert len(result.obligaciones) == 1
    assert result.obligaciones[0].listo is True


async def test_invoke_generar_paquete_evidencias_uploads_and_returns_storage_key(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_con_evidencia: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    res = await db.execute(select(Evidencia).where(Evidencia.actividad_id.isnot(None)))
    ev = res.scalars().first()
    assert ev is not None
    storage = _fake_storage({ev.storage_key: b"contenido-real"})
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.tools.catalog.paquete._get_storage", lambda *_a, **_k: storage)

    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool(
        "generar_paquete_evidencias", ctx, GenerarPaqueteEvidenciasInput(cuenta_id=cuenta_con_evidencia.id)
    )

    assert result.cuenta_cobro_id == cuenta_con_evidencia.id
    assert result.filename.endswith(".zip")
    assert result.size_bytes > 0
    assert result.modo == "standard"
    storage.upload.assert_awaited_once()


async def test_secret_scan_gate_bypassed_when_flag_disabled(
    db: AsyncSession,
    test_user: dict[str, Any],
    cuenta_con_evidencia: CuentaCobro,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "SECRET_SCAN_GATE_ENABLED", False)
    res = await db.execute(select(Evidencia).where(Evidencia.actividad_id.isnot(None)))
    ev = res.scalars().first()
    assert ev is not None
    storage = _fake_storage({ev.storage_key: _LEAK_CORPUS_TEXTO.encode("utf-8")})
    monkeypatch.setattr(informe_service, "_get_storage", lambda *_a, **_k: storage)
    monkeypatch.setattr("app.tools.catalog.paquete._get_storage", lambda *_a, **_k: storage)

    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool(
        "generar_paquete_evidencias", ctx, GenerarPaqueteEvidenciasInput(cuenta_id=cuenta_con_evidencia.id)
    )

    assert result.filename.endswith(".zip")
    assert result.size_bytes > 0
