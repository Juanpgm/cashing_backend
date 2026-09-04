"""Tests for app/tools/catalog/actividades.py (T3) — no tool previously created
actividades at all, so `generar_informe_actividades` had nothing to write about
unless the user connected Google and ran `persistir_evidencias`."""

from __future__ import annotations

from datetime import date

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.exceptions import DomainError, NotFoundError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion, TipoObligacion
from app.models.usuario import Usuario
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"actividades_{suffix}@example.com",
        nombre=f"Actividades User {suffix}",
        cedula=f"6060{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"ACT-{suffix}",
        objeto="Objeto de prueba para actividades",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"6060{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def _crear_cuenta(ctx: ToolContext, contrato_id, mes: int = 3):
    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro",
        ctx,
        {"contrato_id": str(contrato_id), "mes": mes, "anio": 2026},
    )
    return cuenta_response.id


def test_tools_registered() -> None:
    assert "crear_actividades_desde_obligaciones" in TOOL_REGISTRY
    assert "generar_actividades_agente" in TOOL_REGISTRY
    assert "agregar_actividades_desde_texto" in TOOL_REGISTRY


@pytest.mark.asyncio
async def test_crear_actividades_desde_obligaciones_happy_path(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "01")
    db.add(
        Obligacion(
            contrato_id=contrato.id,
            descripcion="Obligación uno con texto suficientemente largo",
            tipo=TipoObligacion.GENERAL,
            orden=0,
        )
    )
    db.add(
        Obligacion(
            contrato_id=contrato.id,
            descripcion="Obligación dos con texto suficientemente largo",
            tipo=TipoObligacion.GENERAL,
            orden=1,
        )
    )
    await db.commit()

    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    result = await invoke_tool("crear_actividades_desde_obligaciones", ctx, {"cuenta_id": str(cuenta_id)})

    assert result.creadas == 2
    assert len(result.actividades) == 2


@pytest.mark.asyncio
async def test_crear_actividades_desde_obligaciones_is_idempotent(db: AsyncSession) -> None:
    """F2 regression: calling this tool TWICE for the same cuenta must not duplicate
    Actividad rows for obligaciones that already have one — those duplicates flow
    straight into the filed informe de actividades / informe de supervisión."""
    from app.models.actividad import Actividad
    from sqlalchemy import select

    user, contrato = await _make_user_with_contrato(db, "10")
    for i in range(3):
        db.add(
            Obligacion(
                contrato_id=contrato.id,
                descripcion=f"Obligación {i} con texto suficientemente largo",
                tipo=TipoObligacion.GENERAL,
                orden=i,
            )
        )
    await db.commit()

    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    first = await invoke_tool("crear_actividades_desde_obligaciones", ctx, {"cuenta_id": str(cuenta_id)})
    assert first.creadas == 3

    second = await invoke_tool("crear_actividades_desde_obligaciones", ctx, {"cuenta_id": str(cuenta_id)})
    assert second.creadas == 0

    rows = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id))
    assert len(rows.scalars().all()) == 3, "second call must not duplicate Actividad rows"


@pytest.mark.asyncio
async def test_crear_actividades_desde_obligaciones_empty_contrato_raises(db: AsyncSession) -> None:
    """A contrato with ZERO obligaciones must raise a clean domain error, not create
    zero activities silently."""
    user, contrato = await _make_user_with_contrato(db, "02")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    with pytest.raises(DomainError, match="no tiene obligaciones"):
        await invoke_tool("crear_actividades_desde_obligaciones", ctx, {"cuenta_id": str(cuenta_id)})


@pytest.mark.asyncio
async def test_crear_actividades_desde_obligaciones_unknown_cuenta_raises_not_found(db: AsyncSession) -> None:
    user, _contrato = await _make_user_with_contrato(db, "03")
    ctx = ToolContext(db=db, usuario=user)

    import uuid

    with pytest.raises(NotFoundError):
        await invoke_tool("crear_actividades_desde_obligaciones", ctx, {"cuenta_id": str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_crear_actividades_desde_obligaciones_rejects_other_users_cuenta(db: AsyncSession) -> None:
    user_a, _contrato_a = await _make_user_with_contrato(db, "04a")
    user_b, contrato_b = await _make_user_with_contrato(db, "04b")
    db.add(
        Obligacion(
            contrato_id=contrato_b.id,
            descripcion="Obligación de B con texto suficientemente largo",
            tipo=TipoObligacion.GENERAL,
            orden=0,
        )
    )
    await db.commit()

    ctx_b = ToolContext(db=db, usuario=user_b)
    cuenta_b_id = await _crear_cuenta(ctx_b, contrato_b.id)

    ctx_a = ToolContext(db=db, usuario=user_a)
    with pytest.raises(DomainError):
        await invoke_tool("crear_actividades_desde_obligaciones", ctx_a, {"cuenta_id": str(cuenta_b_id)})


@pytest.mark.asyncio
async def test_agregar_actividades_desde_texto_happy_path(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "05")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    texto = "1. Elaboré el informe mensual de actividades\n2. Asistí a la reunión de seguimiento del contrato"
    result = await invoke_tool(
        "agregar_actividades_desde_texto",
        ctx,
        {"cuenta_id": str(cuenta_id), "texto": texto, "vincular_obligaciones": False},
    )

    assert result.creadas == 2
    assert len(result.actividades) == 2


@pytest.mark.asyncio
async def test_agregar_actividades_desde_texto_no_numbered_lines_raises(db: AsyncSession) -> None:
    """Free text with no numbered lines must raise a clean domain error, never
    silently create zero activities."""
    user, contrato = await _make_user_with_contrato(db, "06")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    with pytest.raises(DomainError, match="No se encontraron actividades"):
        await invoke_tool(
            "agregar_actividades_desde_texto",
            ctx,
            {"cuenta_id": str(cuenta_id), "texto": "Hice varias cosas este mes pero no las enumeré."},
        )


@pytest.mark.asyncio
async def test_agregar_actividades_desde_texto_defaults(db: AsyncSession) -> None:
    """`vincular_obligaciones` defaults to True and `fecha_realizacion` is optional."""
    user, contrato = await _make_user_with_contrato(db, "07")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    result = await invoke_tool(
        "agregar_actividades_desde_texto",
        ctx,
        {"cuenta_id": str(cuenta_id), "texto": "1. Actividad realizada durante el periodo evaluado"},
    )
    assert result.creadas == 1


@pytest.mark.asyncio
async def test_generar_actividades_agente_no_obligaciones_no_documento_raises(db: AsyncSession) -> None:
    """Without obligaciones OR an uploaded contract document, the LLM path must raise
    a clean domain error pointing to the manual fallback — never call the LLM."""
    user, contrato = await _make_user_with_contrato(db, "08")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    with pytest.raises(DomainError, match=r"desde-texto|actividades manualmente"):
        await invoke_tool("generar_actividades_agente", ctx, {"cuenta_id": str(cuenta_id)})


@pytest.mark.asyncio
async def test_generar_actividades_agente_unknown_cuenta_raises_not_found(db: AsyncSession) -> None:
    user, _contrato = await _make_user_with_contrato(db, "09")
    ctx = ToolContext(db=db, usuario=user)

    import uuid

    with pytest.raises(NotFoundError):
        await invoke_tool("generar_actividades_agente", ctx, {"cuenta_id": str(uuid.uuid4())})
