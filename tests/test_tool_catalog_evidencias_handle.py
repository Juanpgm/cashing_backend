"""Tests for the discovery-handle flow (radicacion-sin-friccion 1.7):
`descubrir_evidencias` stashes its result behind an opaque `handle_id` and
`persistir_evidencias` redeems it instead of taking the full obligaciones list
as a tool argument — see `app.services.evidence_handle_cache`.

`evidence_discovery_service.descubrir_evidencias` is mocked throughout (real
discovery needs Gmail/Drive/Calendar, exercised elsewhere in
`tests/test_evidence_discovery.py`) so these tests stay focused on the
tool-layer handle wiring and `evidence_handle_cache` integration.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.exceptions import DomainError, EvidenceHandleNotFoundError
from app.core.security import hash_password
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.evidencia import Evidencia
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.schemas.google_workspace import EvidenceDiscoveryResponse, EvidenceLink, ObligacionJustificada
from app.services import evidence_handle_cache
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_DISCOVER = "app.tools.catalog.evidencias.evidence_discovery_service.descubrir_evidencias"


async def _make_user_with_contrato(
    db: AsyncSession, suffix: str, n_obligaciones: int = 1
) -> tuple[Usuario, Contrato, list[Obligacion]]:
    user = Usuario(
        email=f"evid_handle_{suffix}@example.com",
        nombre=f"Evidencias Handle User {suffix}",
        cedula=f"4040{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"EVHANDLE-{suffix}",
        objeto="Objeto de prueba para el handle de evidencias",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"4040{suffix}",
    )
    db.add(contrato)
    await db.flush()

    obligaciones = [
        Obligacion(
            contrato_id=contrato.id,
            descripcion=f"Obligación de prueba número {i}",
            tipo=TipoObligacion.ESPECIFICA,
            orden=i,
            etiqueta=f"OE{i + 1}",
        )
        for i in range(n_obligaciones)
    ]
    db.add_all(obligaciones)

    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    for ob in obligaciones:
        await db.refresh(ob)
    return user, contrato, obligaciones


async def _crear_cuenta(ctx: ToolContext, contrato_id: uuid.UUID, mes: int = 3) -> uuid.UUID:
    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro",
        ctx,
        {"contrato_id": str(contrato_id), "mes": mes, "anio": 2026},
    )
    return cuenta_response.id


def _ob_justificada(obligacion: Obligacion, n_links: int = 1) -> ObligacionJustificada:
    return ObligacionJustificada(
        obligacion_id=str(obligacion.id),
        descripcion=obligacion.descripcion,
        actividad=f"Actividad real para {obligacion.etiqueta}",
        justificacion=f"Justificación real para {obligacion.etiqueta}",
        origen="llm",
        evidencias=[
            EvidenceLink(
                source="email",
                titulo=f"Correo de soporte {obligacion.etiqueta}-{j}",
                link=f"https://mail.example.com/{obligacion.id}/{j}",
            )
            for j in range(n_links)
        ],
    )


def _discovery_response(obligaciones: list[ObligacionJustificada]) -> EvidenceDiscoveryResponse:
    total_evidencias = sum(len(ob.evidencias) for ob in obligaciones)
    return EvidenceDiscoveryResponse(
        obligaciones=obligaciones,
        resumen=f"Se encontraron {total_evidencias} evidencias.",
        total_evidencias=total_evidencias,
        fuentes={"email": total_evidencias},
    )


@pytest.fixture(autouse=True)
def _clear_handle_cache():
    evidence_handle_cache.clear()
    yield
    evidence_handle_cache.clear()


@pytest.mark.asyncio
async def test_descubrir_evidencias_returns_a_non_empty_handle_id(db: AsyncSession) -> None:
    user, contrato, obligaciones = await _make_user_with_contrato(db, "01")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    mock_response = _discovery_response([_ob_justificada(obligaciones[0])])
    with patch(_PATCH_DISCOVER, AsyncMock(return_value=mock_response)):
        result = await invoke_tool(
            "descubrir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_id), "fecha_inicio": "2026-03-01", "fecha_fin": "2026-03-31"},
        )

    assert result.handle_id
    uuid.UUID(result.handle_id)  # must be a real UUID-shaped opaque handle
    # Full payload is still returned (REST/MCP consumers rely on this) — the
    # handle is additive, not a replacement of the discovery response shape.
    assert len(result.obligaciones) == 1


@pytest.mark.asyncio
async def test_descubrir_evidencias_does_not_mutate_the_shared_discovery_cache_response(
    db: AsyncSession,
) -> None:
    """`descubrir_evidencias` (service) may be served from `discovery_cache` on a
    repeat call — the tool wrapper must return a COPY with `handle_id` set, never
    mutate the cached response object in place (that would leak a stale handle
    into whatever else reads the same cache entry)."""
    user, contrato, obligaciones = await _make_user_with_contrato(db, "02")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    shared_response = _discovery_response([_ob_justificada(obligaciones[0])])
    assert shared_response.handle_id == ""

    with patch(_PATCH_DISCOVER, AsyncMock(return_value=shared_response)):
        result = await invoke_tool(
            "descubrir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_id), "fecha_inicio": "2026-03-01", "fecha_fin": "2026-03-31"},
        )

    assert result.handle_id != ""
    assert shared_response.handle_id == "", "the object returned by the service must stay untouched"


@pytest.mark.asyncio
async def test_persistir_evidencias_via_handle_creates_rows(db: AsyncSession) -> None:
    user, contrato, obligaciones = await _make_user_with_contrato(db, "03")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    mock_response = _discovery_response([_ob_justificada(obligaciones[0])])
    with patch(_PATCH_DISCOVER, AsyncMock(return_value=mock_response)):
        discovered = await invoke_tool(
            "descubrir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_id), "fecha_inicio": "2026-03-01", "fecha_fin": "2026-03-31"},
        )

    summary = await invoke_tool(
        "persistir_evidencias",
        ctx,
        {"cuenta_id": str(cuenta_id), "handle_id": discovered.handle_id},
    )

    assert summary.actividades_creadas == 1
    assert summary.evidencias_creadas == 1

    actividades = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))).scalars().all()
    assert len(actividades) == 1


@pytest.mark.asyncio
async def test_ten_obligaciones_forty_links_persist_in_full_via_handle(db: AsyncSession) -> None:
    """The exact scenario the handle exists to fix: 10 obligaciones x 4 links each
    persist in full through the handle path — no truncation, no data loss."""
    user, contrato, obligaciones = await _make_user_with_contrato(db, "04", n_obligaciones=10)
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    justificadas = [_ob_justificada(ob, n_links=4) for ob in obligaciones]
    mock_response = _discovery_response(justificadas)
    with patch(_PATCH_DISCOVER, AsyncMock(return_value=mock_response)):
        discovered = await invoke_tool(
            "descubrir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_id), "fecha_inicio": "2026-03-01", "fecha_fin": "2026-03-31"},
        )

    summary = await invoke_tool(
        "persistir_evidencias",
        ctx,
        {"cuenta_id": str(cuenta_id), "handle_id": discovered.handle_id},
    )

    assert summary.actividades_creadas == 10
    assert summary.evidencias_creadas == 40

    actividades = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))).scalars().all()
    assert len(actividades) == 10
    evidencias = (
        (await db.execute(select(Evidencia).where(Evidencia.actividad_id.in_([a.id for a in actividades]))))
        .scalars()
        .all()
    )
    assert len(evidencias) == 40


@pytest.mark.asyncio
async def test_persistir_evidencias_expired_handle_raises_actionable_error(db: AsyncSession) -> None:
    user, contrato, obligaciones = await _make_user_with_contrato(db, "05")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    handle_id = evidence_handle_cache.store(user.id, cuenta_id, [_ob_justificada(obligaciones[0])])
    # Force-expire it directly in the cache instead of sleeping past a real TTL.
    entry = evidence_handle_cache._cache[handle_id]
    import time

    evidence_handle_cache._cache[handle_id] = (time.monotonic() - 1, entry[1])

    with pytest.raises(EvidenceHandleNotFoundError) as exc_info:
        await invoke_tool("persistir_evidencias", ctx, {"cuenta_id": str(cuenta_id), "handle_id": handle_id})

    assert "descubrir_evidencias" in exc_info.value.detail


@pytest.mark.asyncio
async def test_persistir_evidencias_double_redeem_succeeds_without_duplicating_rows(db: AsyncSession) -> None:
    user, contrato, obligaciones = await _make_user_with_contrato(db, "06")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    mock_response = _discovery_response([_ob_justificada(obligaciones[0])])
    with patch(_PATCH_DISCOVER, AsyncMock(return_value=mock_response)):
        discovered = await invoke_tool(
            "descubrir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_id), "fecha_inicio": "2026-03-01", "fecha_fin": "2026-03-31"},
        )

    await invoke_tool("persistir_evidencias", ctx, {"cuenta_id": str(cuenta_id), "handle_id": discovered.handle_id})
    second = await invoke_tool(
        "persistir_evidencias", ctx, {"cuenta_id": str(cuenta_id), "handle_id": discovered.handle_id}
    )

    assert second.evidencias_creadas == 0, "re-persisting the same handle must not duplicate Evidencia rows"

    actividades = (await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))).scalars().all()
    assert len(actividades) == 1
    evidencias = (
        (await db.execute(select(Evidencia).where(Evidencia.actividad_id == actividades[0].id))).scalars().all()
    )
    assert len(evidencias) == 1


@pytest.mark.asyncio
async def test_persistir_evidencias_rejects_handle_from_a_different_user(db: AsyncSession) -> None:
    owner, contrato_owner, obligaciones_owner = await _make_user_with_contrato(db, "07a")
    attacker, _contrato_attacker, _obs_attacker = await _make_user_with_contrato(db, "07b")

    ctx_owner = ToolContext(db=db, usuario=owner)
    cuenta_id = await _crear_cuenta(ctx_owner, contrato_owner.id)

    mock_response = _discovery_response([_ob_justificada(obligaciones_owner[0])])
    with patch(_PATCH_DISCOVER, AsyncMock(return_value=mock_response)):
        discovered = await invoke_tool(
            "descubrir_evidencias",
            ctx_owner,
            {"cuenta_id": str(cuenta_id), "fecha_inicio": "2026-03-01", "fecha_fin": "2026-03-31"},
        )

    ctx_attacker = ToolContext(db=db, usuario=attacker)
    with pytest.raises(DomainError):
        await invoke_tool(
            "persistir_evidencias",
            ctx_attacker,
            {"cuenta_id": str(cuenta_id), "handle_id": discovered.handle_id},
        )


@pytest.mark.asyncio
async def test_persistir_evidencias_rejects_handle_for_a_different_cuenta(db: AsyncSession) -> None:
    user, contrato, obligaciones = await _make_user_with_contrato(db, "08", n_obligaciones=2)
    ctx = ToolContext(db=db, usuario=user)
    cuenta_a = await _crear_cuenta(ctx, contrato.id, mes=1)
    cuenta_b = await _crear_cuenta(ctx, contrato.id, mes=2)

    mock_response = _discovery_response([_ob_justificada(obligaciones[0])])
    with patch(_PATCH_DISCOVER, AsyncMock(return_value=mock_response)):
        discovered = await invoke_tool(
            "descubrir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_a), "fecha_inicio": "2026-03-01", "fecha_fin": "2026-03-31"},
        )

    with pytest.raises(EvidenceHandleNotFoundError):
        await invoke_tool(
            "persistir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_b), "handle_id": discovered.handle_id},
        )


@pytest.mark.asyncio
async def test_persistir_evidencias_malformed_handle_raises_cleanly(db: AsyncSession) -> None:
    user, contrato, _obligaciones = await _make_user_with_contrato(db, "09")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    with pytest.raises(EvidenceHandleNotFoundError):
        await invoke_tool(
            "persistir_evidencias",
            ctx,
            {"cuenta_id": str(cuenta_id), "handle_id": "not-a-real-handle"},
        )
