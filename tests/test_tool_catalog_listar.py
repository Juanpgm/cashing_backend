"""Tests for the `listar_contratos` / `listar_cuentas_cobro` discovery tools.

These are the two tools that let the tool-calling agent discover the user's own
contrato_id / cuenta_id instead of requiring the caller to already know a UUID.
Ownership is the main property under test: a user must never see another
user's contratos or cuentas de cobro, even when explicitly filtering by
someone else's contrato_id.
"""

from __future__ import annotations

import uuid
from datetime import date

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.llm_schema import to_openai_tools
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_user(db: AsyncSession, suffix: str) -> Usuario:
    user = Usuario(
        email=f"listar_{suffix}@example.com",
        nombre=f"Listar User {suffix}",
        cedula=f"4040{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()
    return user


async def _make_contrato(db: AsyncSession, usuario: Usuario, numero: str) -> Contrato:
    contrato = Contrato(
        usuario_id=usuario.id,
        numero_contrato=numero,
        objeto=f"Objeto de prueba para el contrato {numero}",
        entidad="Alcaldía de Prueba",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=usuario.cedula,
    )
    db.add(contrato)
    await db.flush()
    return contrato


@pytest.mark.asyncio
async def test_listar_contratos_only_returns_own_contracts(db: AsyncSession) -> None:
    user_a = await _make_user(db, "a1")
    user_b = await _make_user(db, "b1")
    await _make_contrato(db, user_a, "LC-A-0001")
    await _make_contrato(db, user_b, "LC-B-0001")
    await db.commit()

    ctx_a = ToolContext(db=db, usuario=user_a)
    result_a = await invoke_tool("listar_contratos", ctx_a, {})
    assert [c.numero_contrato for c in result_a.contratos] == ["LC-A-0001"]

    ctx_b = ToolContext(db=db, usuario=user_b)
    result_b = await invoke_tool("listar_contratos", ctx_b, {})
    assert [c.numero_contrato for c in result_b.contratos] == ["LC-B-0001"]


@pytest.mark.asyncio
async def test_listar_contratos_truncates_objeto(db: AsyncSession) -> None:
    user = await _make_user(db, "trunc")
    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="LC-TRUNC-0001",
        objeto="X" * 500,
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=user.cedula,
    )
    db.add(contrato)
    await db.flush()
    await db.commit()

    ctx = ToolContext(db=db, usuario=user)
    result = await invoke_tool("listar_contratos", ctx, {})

    assert len(result.contratos) == 1
    assert len(result.contratos[0].objeto) == 200


@pytest.mark.asyncio
async def test_listar_contratos_empty_for_user_without_contracts(db: AsyncSession) -> None:
    user = await _make_user(db, "empty1")
    await db.commit()

    ctx = ToolContext(db=db, usuario=user)
    result = await invoke_tool("listar_contratos", ctx, {})
    assert result.contratos == []


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_only_returns_own_cuentas(db: AsyncSession) -> None:
    user_a = await _make_user(db, "a2")
    user_b = await _make_user(db, "b2")
    contrato_a = await _make_contrato(db, user_a, "LC-A-0002")
    contrato_b = await _make_contrato(db, user_b, "LC-B-0002")
    await db.commit()

    ctx_a = ToolContext(db=db, usuario=user_a)
    await invoke_tool("crear_cuenta_cobro", ctx_a, {"contrato_id": str(contrato_a.id), "mes": 3, "anio": 2026})
    await db.commit()

    ctx_b = ToolContext(db=db, usuario=user_b)
    await invoke_tool("crear_cuenta_cobro", ctx_b, {"contrato_id": str(contrato_b.id), "mes": 4, "anio": 2026})
    await db.commit()

    result_a = await invoke_tool("listar_cuentas_cobro", ctx_a, {})
    assert len(result_a.cuentas) == 1
    assert result_a.cuentas[0].contrato_id == contrato_a.id

    result_b = await invoke_tool("listar_cuentas_cobro", ctx_b, {})
    assert len(result_b.cuentas) == 1
    assert result_b.cuentas[0].contrato_id == contrato_b.id


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_never_leaks_via_foreign_contrato_id(db: AsyncSession) -> None:
    """Filtering by another user's contrato_id must yield an empty list, never their data."""
    user_a = await _make_user(db, "a3")
    user_b = await _make_user(db, "b3")
    contrato_b = await _make_contrato(db, user_b, "LC-B-0003")
    await db.commit()

    ctx_b = ToolContext(db=db, usuario=user_b)
    await invoke_tool("crear_cuenta_cobro", ctx_b, {"contrato_id": str(contrato_b.id), "mes": 5, "anio": 2026})
    await db.commit()

    ctx_a = ToolContext(db=db, usuario=user_a)
    result = await invoke_tool("listar_cuentas_cobro", ctx_a, {"contrato_id": str(contrato_b.id)})
    assert result.cuentas == []


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_filters_by_contrato_id(db: AsyncSession) -> None:
    user = await _make_user(db, "filter1")
    contrato_1 = await _make_contrato(db, user, "LC-F-0001")
    contrato_2 = await _make_contrato(db, user, "LC-F-0002")
    await db.commit()

    ctx = ToolContext(db=db, usuario=user)
    await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato_1.id), "mes": 1, "anio": 2026})
    await db.commit()
    await invoke_tool("crear_cuenta_cobro", ctx, {"contrato_id": str(contrato_2.id), "mes": 2, "anio": 2026})
    await db.commit()

    result_all = await invoke_tool("listar_cuentas_cobro", ctx, {})
    assert len(result_all.cuentas) == 2

    result_filtered = await invoke_tool("listar_cuentas_cobro", ctx, {"contrato_id": str(contrato_1.id)})
    assert len(result_filtered.cuentas) == 1
    assert result_filtered.cuentas[0].contrato_id == contrato_1.id
    assert result_filtered.cuentas[0].mes == 1


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_empty_for_user_without_cuentas(db: AsyncSession) -> None:
    user = await _make_user(db, "empty2")
    await db.commit()

    ctx = ToolContext(db=db, usuario=user)
    result = await invoke_tool("listar_cuentas_cobro", ctx, {})
    assert result.cuentas == []


@pytest.mark.asyncio
async def test_listar_cuentas_cobro_unknown_contrato_id_yields_empty(db: AsyncSession) -> None:
    user = await _make_user(db, "unknown1")
    await db.commit()

    ctx = ToolContext(db=db, usuario=user)
    result = await invoke_tool("listar_cuentas_cobro", ctx, {"contrato_id": str(uuid.uuid4())})
    assert result.cuentas == []


def test_listar_tools_registered_in_registry() -> None:
    assert "listar_contratos" in TOOL_REGISTRY
    assert "listar_cuentas_cobro" in TOOL_REGISTRY
    assert TOOL_REGISTRY["listar_contratos"].tags == ("read",)
    assert TOOL_REGISTRY["listar_cuentas_cobro"].tags == ("read",)


def test_listar_tools_exported_as_openai_tool_schemas() -> None:
    names = {entry["function"]["name"] for entry in to_openai_tools()}
    assert {"listar_contratos", "listar_cuentas_cobro"} <= names
