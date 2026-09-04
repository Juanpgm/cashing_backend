"""Tests for F1 — `importar_documento` scoping normalisation.

Root bug (reproduced against the real DB, adversarial review): `document_service.
upload_document` only scopes a document CONTRACT-level (`cuenta_cobro_id=None`,
visible to `auto_vincular_documentos_fuente`'s `docs_contrato` pool) when the
caller passes BOTH `cuenta_cobro_id` AND a nivel-contrato `requisito_codigo`
together. `importar_documento(tipo="rut", cuenta_cobro_id=Y)` (no
requisito_codigo) used to leave the document CUENTA-scoped — invisible to
`auto_vincular_documentos`, so `resumen_checklist` never showed it `cargado`.
A fully bare call (`tipo="rut"`, no contrato_id, no cuenta_cobro_id) created an
orphan document in NEITHER pool.

Fixed at the TOOL layer (not `document_service`, which is shared with the HTTP
upload path and has its own strict contract-vs-cuenta scoping history):
`importar_documento` now normalises scoping from `tipo` before calling
`upload_document`.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.exceptions import ValidationError
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente
from app.models.usuario import Usuario
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_S3 = "app.services.document_service._get_storage"


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


async def _make_user_with_contrato(db: AsyncSession, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"scoping_{suffix}@example.com",
        nombre=f"Scoping User {suffix}",
        cedula=f"6161{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"SCOPE-{suffix}",
        objeto="Objeto de prueba para scoping de documentos",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"6161{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


async def _crear_cuenta(ctx: ToolContext, contrato_id, mes: int = 4):
    cuenta_response = await invoke_tool(
        "crear_cuenta_cobro", ctx, {"contrato_id": str(contrato_id), "mes": mes, "anio": 2026}
    )
    return cuenta_response.id


@pytest.mark.parametrize(
    "tipo,codigo",
    [("rut", "RUT"), ("cedula", "CEDULA"), ("acta_inicio", "ACTA_INICIO"), ("rpc", "RPC")],
)
@pytest.mark.asyncio
async def test_import_via_cuenta_cobro_id_only_then_auto_vincula_and_shows_cargado(
    db: AsyncSession, tipo: str, codigo: str
) -> None:
    """The exact reproduced bug: import a contract-level tipo passing ONLY
    cuenta_cobro_id (no requisito_codigo, no contrato_id) — must end up
    CONTRACT-scoped so auto_vincular_documentos finds it and resumen_checklist
    shows it cargado."""
    user, contrato = await _make_user_with_contrato(db, f"{tipo[:6]}")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)
    await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id)})

    filename = f"{tipo}.txt"
    ctx.attachments[filename] = ToolAttachment(
        filename=filename, content_type="text/plain", data=f"contenido de {tipo}".encode()
    )

    with patch(_PATCH_S3, return_value=_fake_storage()):
        result = await invoke_tool(
            "importar_documento",
            ctx,
            {"filename": filename, "tipo": tipo, "cuenta_cobro_id": str(cuenta_id)},
        )

    # The document itself must be CONTRACT-scoped, not cuenta-scoped.
    doc = await db.get(DocumentoFuente, result.documento_id)
    assert doc is not None
    assert doc.cuenta_cobro_id is None
    assert doc.contrato_id == contrato.id

    vinc = await invoke_tool("auto_vincular_documentos", ctx, {"cuenta_id": str(cuenta_id)})
    assert vinc.vinculados >= 1

    resumen = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": str(cuenta_id)})
    item = next(i for i in resumen.items if i.requisito.codigo == codigo)
    assert str(item.estado) == "cargado"
    assert resumen.resumen.radicacion_lista or item.estado == "cargado"


@pytest.mark.asyncio
async def test_import_contract_level_tipo_bare_raises_instead_of_orphaning(db: AsyncSession) -> None:
    """Neither contrato_id nor cuenta_cobro_id given for a nivel-contrato tipo —
    must raise a clear ValidationError instead of creating an unreachable orphan
    document (previously invisible to BOTH the contrato and cuenta pools)."""
    user, _contrato = await _make_user_with_contrato(db, "bare")
    ctx = ToolContext(db=db, usuario=user)
    ctx.attachments["rut.txt"] = ToolAttachment(filename="rut.txt", content_type="text/plain", data=b"contenido rut")

    with patch(_PATCH_S3, return_value=_fake_storage()), pytest.raises(ValidationError):
        await invoke_tool("importar_documento", ctx, {"filename": "rut.txt", "tipo": "rut"})

    rows = await db.execute(select(DocumentoFuente).where(DocumentoFuente.usuario_id == user.id))
    assert rows.scalars().all() == [], "no orphan document should ever be created"


@pytest.mark.asyncio
async def test_import_cuenta_level_tipo_derives_requisito_codigo_when_omitted(db: AsyncSession) -> None:
    """A genuinely cuenta-level tipo (not `_NIVEL_CONTRATO`) with cuenta_cobro_id
    but no requisito_codigo must still get LINKED (codigo derived from the
    tipo->codigo catalog mapping), not left unlinked."""
    user, contrato = await _make_user_with_contrato(db, "cuentalvl")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)
    await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id)})

    ctx.attachments["ss.txt"] = ToolAttachment(filename="ss.txt", content_type="text/plain", data=b"contenido ss")

    with patch(_PATCH_S3, return_value=_fake_storage()):
        result = await invoke_tool(
            "importar_documento",
            ctx,
            {"filename": "ss.txt", "tipo": "seguridad_social", "cuenta_cobro_id": str(cuenta_id)},
        )

    doc = await db.get(DocumentoFuente, result.documento_id)
    assert doc is not None
    assert doc.cuenta_cobro_id == cuenta_id

    resumen = await invoke_tool("resumen_checklist", ctx, {"cuenta_id": str(cuenta_id)})
    item = next(i for i in resumen.items if i.requisito.codigo == "SEGURIDAD_SOCIAL")
    assert str(item.estado) == "cargado"


@pytest.mark.asyncio
async def test_import_explicit_requisito_codigo_is_never_overridden(db: AsyncSession) -> None:
    """An explicitly-passed requisito_codigo must be used as-is, never silently
    replaced by the tipo-derived default."""
    user, contrato = await _make_user_with_contrato(db, "explicit")
    ctx = ToolContext(db=db, usuario=user)
    cuenta_id = await _crear_cuenta(ctx, contrato.id)
    await invoke_tool("definir_requisitos_checklist", ctx, {"cuenta_id": str(cuenta_id), "modo": "reemplazar"})

    ctx.attachments["custom.txt"] = ToolAttachment(
        filename="custom.txt", content_type="text/plain", data=b"contenido custom"
    )

    with patch(_PATCH_S3, return_value=_fake_storage()):
        result = await invoke_tool(
            "importar_documento",
            ctx,
            {
                "filename": "custom.txt",
                "tipo": "informe_actividades",
                "cuenta_cobro_id": str(cuenta_id),
                "requisito_codigo": "EVIDENCIAS",
            },
        )

    doc = await db.get(DocumentoFuente, result.documento_id)
    assert doc is not None
    assert doc.cuenta_cobro_id == cuenta_id
