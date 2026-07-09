"""Regression tests — IDOR fix in document_service.upload_document / importar_documento.

Root cause: the `cuenta_cobro_id` branch resolved `CuentaCobro` by id with no
ownership filter (unlike the `contrato_id` branch just above it), so a user could
pass ANOTHER user's `cuenta_cobro_id` and have their upload silently attach to
(and, with a `requisito_codigo`, mutate the checklist of) a cuenta they don't own.
"""

from __future__ import annotations

from datetime import date

import app.tools.catalog  # noqa: F401 — registers importar_documento
import pytest
from app.core.exceptions import NotFoundError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.documento_fuente import DocumentoFuente
from app.models.usuario import Usuario
from app.services import checklist_service
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_user_with_contrato_and_cuenta(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato, CuentaCobro]:
    user = Usuario(
        email=f"idor_{suffix}@example.com",
        nombre=f"IDOR Test User {suffix}",
        cedula=f"9090{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"IDOR-{suffix}",
        objeto="Objeto de prueba para IDOR",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"9090{suffix}",
    )
    db.add(contrato)
    await db.flush()

    cuenta = CuentaCobro(contrato_id=contrato.id, mes=6, anio=2026, requisitos_modo="estandar")
    db.add(cuenta)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    await db.refresh(cuenta)
    return user, contrato, cuenta


@pytest.mark.asyncio
async def test_importar_documento_rejects_other_users_cuenta_cobro_id(db: AsyncSession) -> None:
    user_a, _contrato_a, _cuenta_a = await _make_user_with_contrato_and_cuenta(db, "0a")
    user_b, _contrato_b, cuenta_b = await _make_user_with_contrato_and_cuenta(db, "0b")

    # Seed cuenta B's checklist and capture its state before the attack attempt.
    filas_before = await checklist_service.asegurar_checklist(db, cuenta_b)
    await db.commit()
    assert filas_before, "expected at least one checklist row for cuenta B"
    requisito_codigo = next(f.requisito_codigo for f in filas_before if f.requisito_codigo is not None)

    attachment = ToolAttachment(
        filename="ataque.txt", content_type="text/plain", data=b"contenido de intento de IDOR"
    )
    ctx_a = ToolContext(db=db, usuario=user_a, attachments={"ataque.txt": attachment})

    with pytest.raises(NotFoundError):
        await invoke_tool(
            "importar_documento",
            ctx_a,
            {
                "filename": "ataque.txt",
                "tipo": "instrucciones",
                "cuenta_cobro_id": str(cuenta_b.id),
                "requisito_codigo": requisito_codigo,
            },
        )

    # No document was created for user A.
    rows = await db.execute(select(DocumentoFuente).where(DocumentoFuente.usuario_id == user_a.id))
    assert rows.scalars().all() == []

    # User B's checklist row is untouched.
    res = await db.execute(
        select(DocumentoCuentaCobro).where(
            DocumentoCuentaCobro.cuenta_cobro_id == cuenta_b.id,
            DocumentoCuentaCobro.requisito_codigo == requisito_codigo,
        )
    )
    fila_after = res.scalar_one()
    assert fila_after.estado == EstadoRequisito.PENDIENTE
    assert fila_after.documento_fuente_id is None

    # Sanity: user B was never referenced anywhere in this assertion set — the test
    # exists purely to prove user A cannot reach user B's data through the tool.
    assert user_b.id != user_a.id


@pytest.mark.asyncio
async def test_importar_documento_rejects_nonexistent_cuenta_cobro_id(db: AsyncSession) -> None:
    user_a, _contrato_a, _cuenta_a = await _make_user_with_contrato_and_cuenta(db, "1a")

    attachment = ToolAttachment(
        filename="ataque2.txt", content_type="text/plain", data=b"contenido"
    )
    ctx_a = ToolContext(db=db, usuario=user_a, attachments={"ataque2.txt": attachment})

    import uuid

    with pytest.raises(NotFoundError):
        await invoke_tool(
            "importar_documento",
            ctx_a,
            {
                "filename": "ataque2.txt",
                "tipo": "instrucciones",
                "cuenta_cobro_id": str(uuid.uuid4()),
            },
        )

    rows = await db.execute(select(DocumentoFuente).where(DocumentoFuente.usuario_id == user_a.id))
    assert rows.scalars().all() == []
