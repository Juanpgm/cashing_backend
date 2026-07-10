"""Tests for app.tools.catalog — the concrete tool wrappers over existing services.

Importing app.tools.catalog registers every catalog tool into TOOL_REGISTRY. These
tests verify the catalog is well-formed (non-empty descriptions, valid JSON
schemas, correct read/write tagging, no auth/payment capabilities) and run a
couple of true end-to-end invocations through invoke_tool against a seeded
in-memory SQLite session, reusing the `db` fixture from tests/conftest.py.
"""

from __future__ import annotations

from datetime import date

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.core.exceptions import CHECKLIST_INCOMPLETE, DomainError, NotFoundError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.schemas.cuenta_cobro import CuentaCobroCreate
from app.tools.catalog.cuentas import CrearCuentaCobroInput
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool, list_tools
from app.tools.registry import TOOL_REGISTRY
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

EXPECTED_TOOL_NAMES = {
    "buscar_secop_por_cedula",
    "importar_contrato_secop",
    "resumen_checklist",
    "detectar_desde_secop",
    "auto_vincular_documentos",
    "generar_informe_actividades",
    "generar_informe_supervision",
    "descubrir_evidencias",
    "persistir_evidencias",
    "crear_cuenta_cobro",
    "radicar_cuenta",
}

FORBIDDEN_NAME_KEYWORDS = (
    "login",
    "register",
    "password",
    "logout",
    "refresh_token",
    "pago",
    "payment",
    "wompi",
    "credito",
    "credit",
    "auth",
)


def test_catalog_registers_expected_tool_names() -> None:
    assert set(TOOL_REGISTRY.keys()) >= EXPECTED_TOOL_NAMES


def test_catalog_tools_have_rich_descriptions_and_valid_schemas() -> None:
    for spec in list_tools():
        if spec.name not in EXPECTED_TOOL_NAMES:
            continue
        assert spec.description.strip(), f"{spec.name} has an empty description"
        assert len(spec.description) > 40, f"{spec.name} description too short for an MCP client"
        assert spec.tags, f"{spec.name} must be tagged read or write"
        assert set(spec.tags) <= {"read", "write"}, f"{spec.name} has unexpected tags: {spec.tags}"

        # Must not raise — this is exactly what an MCP client calls to build its tool schema.
        input_schema = spec.input_model.model_json_schema()
        output_schema = spec.output_model.model_json_schema()
        assert isinstance(input_schema, dict)
        assert isinstance(output_schema, dict)


def test_catalog_never_registers_auth_or_payment_capabilities() -> None:
    for name in TOOL_REGISTRY:
        lowered = name.lower()
        for kw in FORBIDDEN_NAME_KEYWORDS:
            assert kw not in lowered, f"Tool '{name}' looks like an auth/payment/credit-admin capability"


@pytest.mark.asyncio
async def test_invoke_unknown_tool_raises_not_found() -> None:
    ctx = ToolContext(db=None, usuario=None)  # type: ignore[arg-type] — unreachable for the unknown-tool path
    with pytest.raises(NotFoundError):
        await invoke_tool("does_not_exist_tool", ctx, {})


async def _make_user_with_contrato(db: AsyncSession) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email="tool_catalog@example.com",
        nombre="Tool Catalog User",
        cedula="20202020",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="TC-0001",
        objeto="Objeto de prueba para el catálogo de tools",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor="20202020",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.asyncio
async def test_resumen_checklist_end_to_end(db: AsyncSession) -> None:
    """resumen_checklist, invoked through invoke_tool, matches the GET /checklist API
    contract: a freshly created cuenta with requisitos_modo unset returns
    requisitos_definidos=False and no items."""
    user, contrato = await _make_user_with_contrato(db)
    ctx = ToolContext(db=db, usuario=user)

    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro",
        ctx,
        {"contrato_id": str(contrato.id), "mes": 4, "anio": 2026},
    )
    assert cuenta_response.estado.value == "borrador"

    resumen = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": str(cuenta_response.id)})
    assert resumen.requisitos_definidos is False
    assert resumen.items == []


@pytest.mark.asyncio
async def test_crear_cuenta_cobro_then_radicar_incomplete_checklist(db: AsyncSession) -> None:
    """End-to-end through invoke_tool: create a cuenta, then attempt to radicar it
    before the checklist is satisfied — must surface CHECKLIST_INCOMPLETE unchanged,
    proving the tool wrapper propagates the service's domain exception as-is."""
    user, contrato = await _make_user_with_contrato(db)
    ctx = ToolContext(db=db, usuario=user)

    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro",
        ctx,
        {"contrato_id": str(contrato.id), "mes": 5, "anio": 2026},
    )

    with pytest.raises(DomainError) as exc_info:
        await invoke_tool("radicar_cuenta", ctx, {"cuenta_id": str(cuenta_response.id)})
    assert exc_info.value.code == CHECKLIST_INCOMPLETE


class TestCrearCuentaCobroInputMonthNames:
    """`crear_cuenta_cobro` must accept a Spanish month NAME for `mes` — the live bug:
    the user said "creá la cuenta de febrero" and the tool only understood integers.
    The lenient parsing lives ONLY on this tool-specific input model, never on the
    shared REST schema `CuentaCobroCreate`."""

    _CONTRATO_ID = "00000000-0000-0000-0000-000000000000"

    def test_lowercase_month_name_coerced_to_int(self) -> None:
        params = CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes="febrero", anio=2026)
        assert params.mes == 2

    def test_capitalized_month_name_coerced_to_int(self) -> None:
        params = CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes="Diciembre", anio=2026)
        assert params.mes == 12

    def test_numeric_string_coerced_to_int(self) -> None:
        params = CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes="2", anio=2026)
        assert params.mes == 2

    def test_integer_month_passes_through_unchanged(self) -> None:
        params = CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes=7, anio=2026)
        assert params.mes == 7

    def test_setiembre_spelling_variant_accepted(self) -> None:
        params = CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes="setiembre", anio=2026)
        assert params.mes == 9

    def test_unknown_month_name_fails_validation(self) -> None:
        with pytest.raises(ValidationError):
            CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes="xyz", anio=2026)

    def test_out_of_range_numeric_month_still_fails(self) -> None:
        with pytest.raises(ValidationError):
            CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes=13, anio=2026)

    def test_model_dump_builds_a_valid_cuenta_cobro_create(self) -> None:
        """The handler builds `CuentaCobroCreate(**params.model_dump())` — proves that
        round-trip stays valid once `mes` has been coerced to an int."""
        params = CrearCuentaCobroInput(contrato_id=self._CONTRATO_ID, mes="febrero", anio=2026)
        payload = CuentaCobroCreate(**params.model_dump())
        assert payload.mes == 2
        assert payload.anio == 2026

    @pytest.mark.asyncio
    async def test_invoke_tool_end_to_end_with_month_name(self, db: AsyncSession) -> None:
        user = Usuario(
            email="crear_cuenta_mes@example.com",
            nombre="Mes Nombre User",
            cedula="40404040",
            password_hash=hash_password("StrongPass1!"),
            rol="contratista",
            activo=True,
            creditos_disponibles=100,
        )
        db.add(user)
        await db.flush()
        contrato = Contrato(
            usuario_id=user.id,
            numero_contrato="TC-MES-0001",
            objeto="Objeto de prueba para mes por nombre",
            valor_total=12_000_000,
            valor_mensual=1_000_000,
            fecha_inicio=date(2026, 1, 1),
            fecha_fin=date(2026, 12, 31),
            documento_proveedor="40404040",
        )
        db.add(contrato)
        await db.commit()
        await db.refresh(user)
        await db.refresh(contrato)

        ctx = ToolContext(db=db, usuario=user)
        cuenta = await invoke_tool(
            "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato.id), "mes": "febrero", "anio": 2026}
        )
        assert cuenta.mes == 2
        assert cuenta.anio == 2026
