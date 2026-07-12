"""Tests for `adicion_contrato_service` and its tool wrappers (billing-resilience-
templates, slice #4): `registrar_adicion`/`listar_adiciones` (tasks 4.7-4.8) and
`registrar_adicion_contrato`/`listar_adiciones_contrato` (task 4.13).

Model-shape tests live in `tests/test_adiciones_contrato.py`.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.adicion_contrato import TipoAdicion
from app.models.contrato import Contrato
from app.services import adicion_contrato_service
from app.tools.catalog.adiciones import ListarAdicionesContratoInput, RegistrarAdicionContratoInput
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-ADICION-SVC-001",
        objeto="Servicios profesionales para pruebas del servicio de adiciones",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


# ── 4.7/4.8 — registrar_adicion / listar_adiciones ────────────────────────────


async def test_registrar_adicion_persists_new_rpc_cdp(db: AsyncSession, contrato: Contrato) -> None:
    evento = await adicion_contrato_service.registrar_adicion(
        db,
        contrato.usuario_id,
        contrato.id,
        tipo=TipoAdicion.ADICION,
        numero=1,
        fecha_evento=date(2024, 6, 1),
        rpc_nuevo="RPC-200",
        cdp_nuevo="CDP-200",
        valor_adicion=Decimal("2000000.00"),
    )
    await db.commit()

    assert evento.contrato_id == contrato.id
    assert evento.rpc_nuevo == "RPC-200"
    assert evento.valor_adicion == Decimal("2000000.00")

    eventos = await adicion_contrato_service.listar_adiciones(db, contrato.usuario_id, contrato.id)
    assert len(eventos) == 1
    assert eventos[0].id == evento.id


async def test_listar_adiciones_preserves_order_second_does_not_overwrite_first(
    db: AsyncSession, contrato: Contrato
) -> None:
    primero = await adicion_contrato_service.registrar_adicion(
        db,
        contrato.usuario_id,
        contrato.id,
        tipo=TipoAdicion.ADICION,
        numero=1,
        fecha_evento=date(2024, 3, 1),
        rpc_nuevo="RPC-100",
    )
    await db.commit()
    segundo = await adicion_contrato_service.registrar_adicion(
        db,
        contrato.usuario_id,
        contrato.id,
        tipo=TipoAdicion.ADICION,
        numero=2,
        fecha_evento=date(2024, 9, 1),
        rpc_nuevo="RPC-200",
    )
    await db.commit()

    eventos = await adicion_contrato_service.listar_adiciones(db, contrato.usuario_id, contrato.id)
    assert [e.id for e in eventos] == [primero.id, segundo.id]
    assert eventos[0].rpc_nuevo == "RPC-100"
    assert eventos[1].rpc_nuevo == "RPC-200"


async def test_registrar_adicion_handles_none_valor_adicion(db: AsyncSession, contrato: Contrato) -> None:
    """Cross-change note (task 4.8): the SECOP source's `valor_adicion` may be
    None — recording an event without a value must not crash or coerce to 0."""
    evento = await adicion_contrato_service.registrar_adicion(
        db,
        contrato.usuario_id,
        contrato.id,
        tipo=TipoAdicion.ADICION,
        numero=1,
        fecha_evento=date(2024, 6, 1),
        valor_adicion=None,
    )
    await db.commit()
    assert evento.valor_adicion is None


async def test_registrar_adicion_raises_not_found_for_missing_contrato(db: AsyncSession) -> None:
    with pytest.raises(NotFoundError):
        await adicion_contrato_service.registrar_adicion(
            db,
            uuid.uuid4(),
            uuid.uuid4(),
            tipo=TipoAdicion.PRORROGA,
            numero=1,
            fecha_evento=date(2024, 6, 1),
        )


async def test_registrar_adicion_raises_forbidden_for_other_user(db: AsyncSession, contrato: Contrato) -> None:
    with pytest.raises(ForbiddenError):
        await adicion_contrato_service.registrar_adicion(
            db,
            uuid.uuid4(),
            contrato.id,
            tipo=TipoAdicion.PRORROGA,
            numero=1,
            fecha_evento=date(2024, 6, 1),
        )


async def test_listar_adiciones_raises_forbidden_for_other_user(db: AsyncSession, contrato: Contrato) -> None:
    with pytest.raises(ForbiddenError):
        await adicion_contrato_service.listar_adiciones(db, uuid.uuid4(), contrato.id)


# ── 4.13 — tool wrappers ──────────────────────────────────────────────────────


def test_registrar_adicion_contrato_is_registered_as_write_tool() -> None:
    assert "registrar_adicion_contrato" in TOOL_REGISTRY
    assert TOOL_REGISTRY["registrar_adicion_contrato"].tags == ("write",)


def test_listar_adiciones_contrato_is_registered_as_read_tool() -> None:
    assert "listar_adiciones_contrato" in TOOL_REGISTRY
    assert TOOL_REGISTRY["listar_adiciones_contrato"].tags == ("read",)


async def test_invoke_registrar_adicion_contrato_persists_event(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool(
        "registrar_adicion_contrato",
        ctx,
        RegistrarAdicionContratoInput(
            contrato_id=contrato.id,
            tipo=TipoAdicion.PRORROGA,
            numero=1,
            fecha_evento=date(2024, 6, 1),
            nueva_fecha_fin=date(2025, 3, 31),
        ),
    )
    await db.commit()

    assert result.contrato_id == contrato.id
    assert result.tipo == TipoAdicion.PRORROGA
    assert result.nueva_fecha_fin == date(2025, 3, 31)


async def test_invoke_listar_adiciones_contrato_returns_ordered_history(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    await adicion_contrato_service.registrar_adicion(
        db, contrato.usuario_id, contrato.id, tipo=TipoAdicion.ADICION, numero=1, fecha_evento=date(2024, 3, 1)
    )
    await db.commit()
    await adicion_contrato_service.registrar_adicion(
        db, contrato.usuario_id, contrato.id, tipo=TipoAdicion.PRORROGA, numero=2, fecha_evento=date(2024, 9, 1)
    )
    await db.commit()

    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool("listar_adiciones_contrato", ctx, ListarAdicionesContratoInput(contrato_id=contrato.id))

    assert len(result.adiciones) == 2
    assert result.adiciones[0].tipo == TipoAdicion.ADICION
    assert result.adiciones[1].tipo == TipoAdicion.PRORROGA
