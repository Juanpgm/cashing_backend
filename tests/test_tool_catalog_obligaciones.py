"""Tests for `extraer_obligaciones_contrato` (app/tools/catalog/obligaciones.py, T4)
— the recovery path when a contrato has no obligaciones registered and no tool
could previously re-trigger extraction from an already-imported contract document.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.exceptions import DomainError, NotFoundError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_S3 = "app.services.document_service._get_storage"

_OBLIGACIONES_VERBATIM = [
    "Elaborar los informes mensuales de actividades dentro de los plazos establecidos por la entidad",
    "Asistir a las reuniones de seguimiento convocadas por el supervisor del contrato mensualmente",
    "Entregar los soportes de seguridad social junto con cada cuenta de cobro presentada",
]

_TEXTO_CONTRATO = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº CTR-OBL-0001

CLÁUSULA PRIMERA — OBJETO: El contratista se obliga a prestar sus servicios
profesionales para apoyar la gestión administrativa de la entidad.

CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

1. {_OBLIGACIONES_VERBATIM[0]}.
2. {_OBLIGACIONES_VERBATIM[1]}.
3. {_OBLIGACIONES_VERBATIM[2]}.

CLÁUSULA TERCERA — VALOR DEL CONTRATO: Doce millones de pesos.
""".strip()


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"obligaciones_{suffix}@example.com",
        nombre=f"Obligaciones User {suffix}",
        cedula=f"8080{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"CTR-OBL-{suffix}",
        objeto="Objeto de prueba para extraccion de obligaciones",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"8080{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


def test_tool_registered() -> None:
    assert "extraer_obligaciones_contrato" in TOOL_REGISTRY


@pytest.mark.asyncio
async def test_extraer_obligaciones_contrato_happy_path(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "01")
    ctx = ToolContext(db=db, usuario=user)

    attachment = ToolAttachment(
        filename="contrato.txt", content_type="text/plain", data=_TEXTO_CONTRATO.encode("utf-8")
    )
    ctx.attachments["contrato.txt"] = attachment
    with patch(_PATCH_S3, return_value=_fake_storage()):
        await invoke_tool(
            "importar_documento",
            ctx,
            {"filename": "contrato.txt", "tipo": "contrato", "contrato_id": str(contrato.id)},
        )

    result = await invoke_tool("extraer_obligaciones_contrato", ctx, {"contrato_id": str(contrato.id)})

    assert result.contrato_id == contrato.id
    assert result.total >= 1
    assert len(result.obligaciones) == result.total


@pytest.mark.asyncio
async def test_extraer_obligaciones_contrato_no_documento_raises_not_found(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "02")
    ctx = ToolContext(db=db, usuario=user)

    with pytest.raises(NotFoundError):
        await invoke_tool("extraer_obligaciones_contrato", ctx, {"contrato_id": str(contrato.id)})


@pytest.mark.asyncio
async def test_extraer_obligaciones_contrato_unknown_contrato_raises_not_found(db: AsyncSession) -> None:
    user, _contrato = await _make_user_with_contrato(db, "03")
    ctx = ToolContext(db=db, usuario=user)

    with pytest.raises(NotFoundError):
        await invoke_tool("extraer_obligaciones_contrato", ctx, {"contrato_id": str(uuid.uuid4())})


@pytest.mark.asyncio
async def test_extraer_obligaciones_contrato_rejects_other_users_contrato(db: AsyncSession) -> None:
    user_a, _contrato_a = await _make_user_with_contrato(db, "04a")
    user_b, contrato_b = await _make_user_with_contrato(db, "04b")

    ctx_b = ToolContext(db=db, usuario=user_b)
    attachment = ToolAttachment(
        filename="contrato_b.txt", content_type="text/plain", data=_TEXTO_CONTRATO.encode("utf-8")
    )
    ctx_b.attachments["contrato_b.txt"] = attachment
    with patch(_PATCH_S3, return_value=_fake_storage()):
        await invoke_tool(
            "importar_documento",
            ctx_b,
            {"filename": "contrato_b.txt", "tipo": "contrato", "contrato_id": str(contrato_b.id)},
        )

    ctx_a = ToolContext(db=db, usuario=user_a)
    with pytest.raises((NotFoundError, DomainError)):
        await invoke_tool("extraer_obligaciones_contrato", ctx_a, {"contrato_id": str(contrato_b.id)})
