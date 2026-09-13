"""Tests for the phase-1 agent "escape hatch" tools (radicacion-sin-friccion slice 1.6):
`marcar_requisito`, `obtener_estado_radicacion`, `generar_cuenta_cobro_pdf`, `editar_actividad`.

All four are thin wrappers over existing services (`checklist_service`,
`stepper_state_service`, `cuenta_cobro_service`, `actividad_service`) — these tests
exercise them through `invoke_tool`, the same call site the MCP server/agent graph use.
"""

from __future__ import annotations

import unittest.mock as mock
import uuid
from datetime import date
from unittest.mock import AsyncMock

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.core.exceptions import DomainError, ForbiddenError, NotFoundError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.usuario import Usuario
from app.services import checklist_service, cuenta_cobro_service
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

NEW_TOOL_NAMES = {
    "marcar_requisito",
    "obtener_estado_radicacion",
    "generar_cuenta_cobro_pdf",
    "editar_actividad",
}


def test_new_tools_registered_with_valid_schemas() -> None:
    for name in NEW_TOOL_NAMES:
        assert name in TOOL_REGISTRY, f"{name} not registered"
        spec = TOOL_REGISTRY[name]
        assert spec.description.strip()
        assert len(spec.description) > 40
        assert spec.tags
        assert set(spec.tags) <= {"read", "write"}
        assert isinstance(spec.input_model.model_json_schema(), dict)
        assert isinstance(spec.output_model.model_json_schema(), dict)


async def _make_user_with_contrato(
    db: AsyncSession, email: str = "escape_hatch@example.com"
) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=email,
        nombre="Escape Hatch User",
        cedula="30303030",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="EH-0001",
        objeto="Objeto de prueba para escape hatch tools",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor="30303030",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def _cuenta_con_checklist_definido(
    db: AsyncSession, ctx: ToolContext, contrato: Contrato, mes: int
) -> CuentaCobro:
    """Create a cuenta and resolve the requisitos-modo gate (estandar)."""
    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": mes, "anio": 2026}
    )
    await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_response.id)})
    result = await db.get(CuentaCobro, cuenta_response.id)
    assert result is not None
    return result


# ── marcar_requisito ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_marcar_requisito_no_aplica_end_to_end(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db)
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=1)

    item = await invoke_tool(
        "marcar_requisito",
        ctx,
        {"cuenta_id": str(cuenta.id), "codigo": "DS_CONSECUTIVO", "modo": "no_aplica"},
    )

    assert item.estado.value == "no_aplica"


@pytest.mark.asyncio
async def test_marcar_requisito_cumplido_manual_with_observaciones(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_2@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=2)

    item = await invoke_tool(
        "marcar_requisito",
        ctx,
        {
            "cuenta_id": str(cuenta.id),
            "codigo": "COMPROBANTE_PAGO_SS",
            "modo": "cumplido_manual",
            "observaciones": "Pago verificado por fuera del sistema.",
        },
    )

    assert item.estado.value == "cumplido_manual"
    assert item.observaciones == "Pago verificado por fuera del sistema."


@pytest.mark.asyncio
async def test_marcar_requisito_desvincular_resets_to_pendiente(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_3@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=3)

    await invoke_tool(
        "marcar_requisito",
        ctx,
        {"cuenta_id": str(cuenta.id), "codigo": "DS_CONSECUTIVO", "modo": "cumplido_manual"},
    )
    item = await invoke_tool(
        "marcar_requisito",
        ctx,
        {"cuenta_id": str(cuenta.id), "codigo": "DS_CONSECUTIVO", "modo": "desvincular"},
    )

    assert item.estado.value == "pendiente"


@pytest.mark.asyncio
async def test_marcar_requisito_rejected_on_cuenta_ya_enviada(db: AsyncSession) -> None:
    """Edge case (plan-named): mutating the checklist of an already-submitted
    cuenta must be rejected, not silently accepted — matches the BORRADOR/RECHAZADA
    editability guard used everywhere else in cuenta_cobro_service."""
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_4@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=4)

    cuenta.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()

    with pytest.raises(DomainError):
        await invoke_tool(
            "marcar_requisito",
            ctx,
            {"cuenta_id": str(cuenta.id), "codigo": "DS_CONSECUTIVO", "modo": "no_aplica"},
        )


@pytest.mark.asyncio
async def test_marcar_requisito_cross_user_returns_forbidden(db: AsyncSession) -> None:
    owner, contrato = await _make_user_with_contrato(db, email="escape_hatch_owner@example.com")
    owner_ctx = ToolContext(db=db, usuario=owner)
    cuenta = await _cuenta_con_checklist_definido(db, owner_ctx, contrato, mes=5)

    intruder, _ = await _make_user_with_contrato(db, email="escape_hatch_intruder@example.com")
    intruder_ctx = ToolContext(db=db, usuario=intruder)

    # `cuenta_cobro_service._get_cuenta_con_ownership` (existing, shared by every
    # CuentaCobro-scoped tool) raises ForbiddenError for an existing-but-not-owned
    # cuenta and NotFoundError only when the row truly doesn't exist — established
    # codebase convention (see tests/test_cuenta_cobro_service.py), not something
    # this slice changes. Either way it's a domain exception, never a raw 500/leak.
    with pytest.raises(ForbiddenError):
        await invoke_tool(
            "marcar_requisito",
            intruder_ctx,
            {"cuenta_id": str(cuenta.id), "codigo": "DS_CONSECUTIVO", "modo": "no_aplica"},
        )


@pytest.mark.asyncio
async def test_marcar_requisito_invalid_modo_is_validation_error_not_500(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_5@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=6)

    with pytest.raises(PydanticValidationError):
        await invoke_tool(
            "marcar_requisito",
            ctx,
            {"cuenta_id": str(cuenta.id), "codigo": "DS_CONSECUTIVO", "modo": "estado_inventado"},
        )


@pytest.mark.asyncio
async def test_marcar_requisito_full_round_trip_unblocks_checklist(db: AsyncSession) -> None:
    """Mark every obligatorio requisito as cumplido_manual except one, then use
    marcar_requisito(no_aplica) on the last one — resumen_checklist must
    immediately report radicacion_lista=True with zero pendientes."""
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_6@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=7)

    catalogo = await checklist_service.listar_catalogo(db)
    obligatorios = [r.codigo for r in catalogo if r.obligatorio]
    assert len(obligatorios) >= 1
    ultimo = obligatorios[-1]

    for codigo in obligatorios[:-1]:
        await checklist_service.marcar_cumplido_manual(db, cuenta.id, codigo)
    await db.commit()

    await invoke_tool(
        "marcar_requisito",
        ctx,
        {"cuenta_id": str(cuenta.id), "codigo": ultimo, "modo": "no_aplica"},
    )

    resumen = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": str(cuenta.id)})
    assert resumen.resumen.pendientes == 0
    assert resumen.resumen.radicacion_lista is True


# ── obtener_estado_radicacion ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_obtener_estado_radicacion_thin_wrap_of_stepper_state(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_7@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=8)

    estado = await invoke_tool("obtener_estado_radicacion", ctx, {"cuenta_id": str(cuenta.id)})

    assert estado.cuenta_cobro_id == cuenta.id
    assert len(estado.steps) == 7


@pytest.mark.asyncio
async def test_obtener_estado_radicacion_on_undefined_requisitos_modo_does_not_crash(db: AsyncSession) -> None:
    """A freshly created cuenta (requisitos_modo still NULL) must not crash the
    stepper-state aggregate — step 3 stays incomplete/blocking instead."""
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_8@example.com")
    ctx = ToolContext(db=db, usuario=user)

    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 9, "anio": 2026}
    )

    estado = await invoke_tool("obtener_estado_radicacion", ctx, {"cuenta_id": str(cuenta_response.id)})

    step3 = next(s for s in estado.steps if s.step == 3)
    assert step3.complete is False
    assert step3.code == "CHECKLIST_INCOMPLETE"


@pytest.mark.asyncio
async def test_obtener_estado_radicacion_cross_user_returns_not_found(db: AsyncSession) -> None:
    owner, contrato = await _make_user_with_contrato(db, email="escape_hatch_owner2@example.com")
    owner_ctx = ToolContext(db=db, usuario=owner)
    cuenta = await _cuenta_con_checklist_definido(db, owner_ctx, contrato, mes=10)

    intruder, _ = await _make_user_with_contrato(db, email="escape_hatch_intruder2@example.com")
    intruder_ctx = ToolContext(db=db, usuario=intruder)

    with pytest.raises(ForbiddenError):
        await invoke_tool("obtener_estado_radicacion", intruder_ctx, {"cuenta_id": str(cuenta.id)})


# ── generar_cuenta_cobro_pdf ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generar_cuenta_cobro_pdf_uploads_and_returns_url(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_9@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=11)

    fake_storage = AsyncMock()
    fake_storage.upload = AsyncMock(return_value=f"pdfs/{user.id}/{cuenta.id}.pdf")
    fake_storage.presigned_url = AsyncMock(return_value="https://storage.example.com/presigned")
    monkeypatch.setattr("app.tools.catalog.cuentas._get_storage", lambda *_a, **_k: fake_storage)

    with mock.patch.object(cuenta_cobro_service, "generate_pdf_from_html", return_value=b"%PDF-fake"):
        resp = await invoke_tool("generar_cuenta_cobro_pdf", ctx, {"cuenta_id": str(cuenta.id)})

    assert resp.pdf_url == "https://storage.example.com/presigned"
    fake_storage.upload.assert_called_once()


@pytest.mark.asyncio
async def test_generar_cuenta_cobro_pdf_on_incomplete_checklist_matches_service_behavior(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service layer does not gate PDF generation on checklist completeness —
    the tool must not add a new restriction that isn't already there."""
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_10@example.com")
    ctx = ToolContext(db=db, usuario=user)
    # requisitos_modo left NULL — checklist is deliberately undefined/incomplete.
    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": 12, "anio": 2026}
    )

    fake_storage = AsyncMock()
    fake_storage.upload = AsyncMock(return_value=f"pdfs/{user.id}/{cuenta_response.id}.pdf")
    fake_storage.presigned_url = AsyncMock(return_value="https://storage.example.com/presigned")
    monkeypatch.setattr("app.tools.catalog.cuentas._get_storage", lambda *_a, **_k: fake_storage)

    with mock.patch.object(cuenta_cobro_service, "generate_pdf_from_html", return_value=b"%PDF-fake"):
        resp = await invoke_tool("generar_cuenta_cobro_pdf", ctx, {"cuenta_id": str(cuenta_response.id)})

    assert resp.pdf_url == "https://storage.example.com/presigned"


@pytest.mark.asyncio
async def test_generar_cuenta_cobro_pdf_cross_user_returns_not_found(db: AsyncSession) -> None:
    owner, contrato = await _make_user_with_contrato(db, email="escape_hatch_owner3@example.com")
    owner_ctx = ToolContext(db=db, usuario=owner)
    cuenta = await _cuenta_con_checklist_definido(db, owner_ctx, contrato, mes=1)

    intruder, _ = await _make_user_with_contrato(db, email="escape_hatch_intruder3@example.com")
    intruder_ctx = ToolContext(db=db, usuario=intruder)

    with pytest.raises(ForbiddenError):
        await invoke_tool("generar_cuenta_cobro_pdf", intruder_ctx, {"cuenta_id": str(cuenta.id)})


# ── editar_actividad ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_editar_actividad_updates_descripcion(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_11@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=2)

    bulk = await invoke_tool(
        "agregar_actividades_desde_texto",
        ctx,
        {"cuenta_id": str(cuenta.id), "texto": "1. Actividad original de prueba"},
    )
    assert bulk.creadas == 1

    actividad_id = (await invoke_tool("resumen_checklist", ctx, {"cuenta_id": str(cuenta.id)})).cuenta_cobro_id
    # resumen_checklist doesn't carry actividades; fetch via the model directly.
    from app.models.actividad import Actividad
    from sqlalchemy import select

    res = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividad = res.scalars().one()

    updated = await invoke_tool(
        "editar_actividad",
        ctx,
        {
            "cuenta_id": str(cuenta.id),
            "actividad_id": str(actividad.id),
            "descripcion": "Descripcion editada por el agente para la actividad",
        },
    )

    assert updated.descripcion == "Descripcion editada por el agente para la actividad"
    assert actividad_id == cuenta.id  # sanity: same cuenta scope used throughout


@pytest.mark.asyncio
async def test_editar_actividad_rejects_field_the_endpoint_does_not_allow(db: AsyncSession) -> None:
    """`descripcion` has min_length=10 at the schema level — a too-short value is a
    clear validation rejection, not a 500, and not silently truncated/accepted."""
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_12@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=3)

    await invoke_tool(
        "agregar_actividades_desde_texto",
        ctx,
        {"cuenta_id": str(cuenta.id), "texto": "1. Actividad original de prueba"},
    )
    from app.models.actividad import Actividad
    from sqlalchemy import select

    res = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividad = res.scalars().one()

    with pytest.raises(PydanticValidationError):
        await invoke_tool(
            "editar_actividad",
            ctx,
            {"cuenta_id": str(cuenta.id), "actividad_id": str(actividad.id), "descripcion": "corta"},
        )


@pytest.mark.asyncio
async def test_editar_actividad_cross_user_returns_not_found(db: AsyncSession) -> None:
    owner, contrato = await _make_user_with_contrato(db, email="escape_hatch_owner4@example.com")
    owner_ctx = ToolContext(db=db, usuario=owner)
    cuenta = await _cuenta_con_checklist_definido(db, owner_ctx, contrato, mes=4)

    await invoke_tool(
        "agregar_actividades_desde_texto",
        owner_ctx,
        {"cuenta_id": str(cuenta.id), "texto": "1. Actividad original de prueba"},
    )
    from app.models.actividad import Actividad
    from sqlalchemy import select

    res = await db.execute(select(Actividad).where(Actividad.cuenta_cobro_id == cuenta.id))
    actividad = res.scalars().one()

    intruder, _ = await _make_user_with_contrato(db, email="escape_hatch_intruder4@example.com")
    intruder_ctx = ToolContext(db=db, usuario=intruder)

    with pytest.raises(NotFoundError):
        await invoke_tool(
            "editar_actividad",
            intruder_ctx,
            {"cuenta_id": str(cuenta.id), "actividad_id": str(actividad.id), "descripcion": "Intento ajeno de edicion"},
        )


@pytest.mark.asyncio
async def test_editar_actividad_random_actividad_id_returns_not_found(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, email="escape_hatch_13@example.com")
    ctx = ToolContext(db=db, usuario=user)
    cuenta = await _cuenta_con_checklist_definido(db, ctx, contrato, mes=5)

    with pytest.raises(NotFoundError):
        await invoke_tool(
            "editar_actividad",
            ctx,
            {
                "cuenta_id": str(cuenta.id),
                "actividad_id": str(uuid.uuid4()),
                "descripcion": "No deberia encontrar esta actividad inexistente",
            },
        )
