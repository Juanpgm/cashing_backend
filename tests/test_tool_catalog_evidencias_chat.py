"""Tests for `subir_evidencias_desde_chat` (T5) — uploads chat-attached files as
evidencia when the user has no Gmail/Drive/Calendar connected (the only prior
evidence path, `descubrir_evidencias`, requires it)."""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.exceptions import DomainError, NotFoundError, ValidationError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.usuario import Usuario
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_STORAGE = "app.tools.catalog.evidencias._get_storage"


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    storage.presigned_url = AsyncMock(return_value="https://example.com/presigned")
    return storage


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"evid_chat_{suffix}@example.com",
        nombre=f"Evidencias Chat User {suffix}",
        cedula=f"3030{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"EVCHAT-{suffix}",
        objeto="Objeto de prueba para evidencias desde chat",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"3030{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def _crear_cuenta(ctx: ToolContext, contrato_id: uuid.UUID, mes: int = 2):
    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro",
        ctx,
        {"contrato_id": str(contrato_id), "mes": mes, "anio": 2026},
    )
    return cuenta_response.id


def test_tool_registered() -> None:
    assert "subir_evidencias_desde_chat" in TOOL_REGISTRY


@pytest.mark.asyncio
async def test_subir_evidencias_desde_chat_happy_path(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "01")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    ctx.attachments["evidencia1.txt"] = ToolAttachment(
        filename="evidencia1.txt", content_type="text/plain", data=b"contenido de evidencia uno"
    )
    ctx.attachments["evidencia2.txt"] = ToolAttachment(
        filename="evidencia2.txt", content_type="text/plain", data=b"contenido de evidencia dos"
    )

    with patch(_PATCH_STORAGE, return_value=_fake_storage()):
        result = await invoke_tool(
            "subir_evidencias_desde_chat",
            ctx,
            {"cuenta_id": str(cuenta_id), "filenames": ["evidencia1.txt", "evidencia2.txt"]},
        )

    assert len(result.resultados) == 2
    nombres = {r.nombre_archivo for r in result.resultados}
    assert nombres == {"evidencia1.txt", "evidencia2.txt"}


@pytest.mark.asyncio
async def test_subir_evidencias_desde_chat_missing_attachment_raises_not_found(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "02")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    ctx.attachments["real.txt"] = ToolAttachment(filename="real.txt", content_type="text/plain", data=b"contenido")

    with patch(_PATCH_STORAGE, return_value=_fake_storage()), pytest.raises(NotFoundError):
        await invoke_tool(
            "subir_evidencias_desde_chat",
            ctx,
            {"cuenta_id": str(cuenta_id), "filenames": ["no_existe.txt"]},
        )


@pytest.mark.asyncio
async def test_subir_evidencias_desde_chat_unknown_cuenta_raises_not_found(db: AsyncSession) -> None:
    user, _contrato = await _make_user_with_contrato(db, "03")
    ctx = ToolContext(db=db, usuario=user)
    ctx.attachments["real.txt"] = ToolAttachment(filename="real.txt", content_type="text/plain", data=b"contenido")

    with patch(_PATCH_STORAGE, return_value=_fake_storage()), pytest.raises(NotFoundError):
        await invoke_tool(
            "subir_evidencias_desde_chat",
            ctx,
            {"cuenta_id": str(uuid.uuid4()), "filenames": ["real.txt"]},
        )


@pytest.mark.asyncio
async def test_subir_evidencias_desde_chat_rejects_other_users_cuenta(db: AsyncSession) -> None:
    user_a, _contrato_a = await _make_user_with_contrato(db, "04a")
    user_b, contrato_b = await _make_user_with_contrato(db, "04b")

    ctx_b = ToolContext(db=db, usuario=user_b)
    cuenta_b_id = await _crear_cuenta(ctx_b, contrato_b.id)

    ctx_a = ToolContext(db=db, usuario=user_a)
    ctx_a.attachments["archivo.txt"] = ToolAttachment(
        filename="archivo.txt", content_type="text/plain", data=b"contenido de intento IDOR"
    )

    with patch(_PATCH_STORAGE, return_value=_fake_storage()), pytest.raises(DomainError):
        await invoke_tool(
            "subir_evidencias_desde_chat",
            ctx_a,
            {"cuenta_id": str(cuenta_b_id), "filenames": ["archivo.txt"]},
        )


@pytest.mark.asyncio
async def test_subir_evidencias_desde_chat_empty_filenames_raises(db: AsyncSession) -> None:
    user, contrato = await _make_user_with_contrato(db, "05")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)

    with patch(_PATCH_STORAGE, return_value=_fake_storage()), pytest.raises(ValidationError):
        await invoke_tool(
            "subir_evidencias_desde_chat",
            ctx,
            {"cuenta_id": str(cuenta_id), "filenames": []},
        )
