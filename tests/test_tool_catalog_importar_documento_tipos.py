"""Tests for the widened `importar_documento.tipo` (T2).

Root problem: the tool only accepted tipo in {"contrato", "instrucciones",
"plantilla"} while `TipoDocumentoFuente` has 15 members — the mandatory
checklist requisitos (RPC, SEGURIDAD_SOCIAL, CEDULA, RUT, ACTA_INICIO, ...)
could never be imported from chat.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import app.tools.catalog  # noqa: F401 — registers every catalog tool
import pytest
from app.core.security import hash_password
from app.models.contrato import Contrato
from app.models.documento_fuente import TipoDocumentoFuente
from app.models.usuario import Usuario
from app.tools.catalog.importar_documento import ImportarDocumentoInput
from app.tools.context import ToolAttachment, ToolContext
from app.tools.invoke import invoke_tool
from app.tools.llm_schema import to_openai_tools
from app.tools.registry import TOOL_REGISTRY

_PATCH_S3 = "app.services.document_service._get_storage"


def _fake_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock(return_value="fake/key")
    return storage


def test_importar_documento_schema_enumerates_every_tipo_documento_fuente() -> None:
    schema = ImportarDocumentoInput.model_json_schema()
    tipo_schema = schema["properties"]["tipo"]

    # No unresolved $ref — the LLM adapter must see the enum inline.
    assert "$ref" not in tipo_schema
    assert "allOf" not in tipo_schema

    assert set(tipo_schema["enum"]) == {t.value for t in TipoDocumentoFuente}
    assert tipo_schema.get("default") == "contrato"


def test_to_openai_tools_produces_inline_enum_for_importar_documento() -> None:
    tools = {t["function"]["name"]: t for t in to_openai_tools(TOOL_REGISTRY)}
    parametros = tools["importar_documento"]["function"]["parameters"]
    tipo_schema = parametros["properties"]["tipo"]
    assert "$ref" not in tipo_schema
    assert set(tipo_schema["enum"]) == {t.value for t in TipoDocumentoFuente}


async def _make_user_with_contrato(db, suffix: str) -> tuple[Usuario, Contrato]:
    user = Usuario(
        email=f"tipos_{suffix}@example.com",
        nombre=f"Tipos User {suffix}",
        cedula=f"5050{suffix}",
        password_hash=hash_password("StrongPass1!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.flush()

    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato=f"TIPOS-{suffix}",
        objeto="Objeto de prueba para tipos",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        documento_proveedor=f"5050{suffix}",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(user)
    await db.refresh(contrato)
    return user, contrato


@pytest.mark.parametrize(
    "tipo",
    ["rpc", "cedula", "rut", "acta_inicio", "seguridad_social"],
)
@pytest.mark.asyncio
async def test_importar_documento_accepts_each_newly_exposed_tipo(db, tipo: str) -> None:
    user, contrato = await _make_user_with_contrato(db, tipo[:8])
    attachment = ToolAttachment(filename=f"{tipo}.txt", content_type="text/plain", data=b"contenido de prueba")
    ctx = ToolContext(db=db, usuario=user, attachments={f"{tipo}.txt": attachment})

    with patch(_PATCH_S3, return_value=_fake_storage()):
        result = await invoke_tool(
            "importar_documento",
            ctx,
            {"filename": f"{tipo}.txt", "tipo": tipo, "contrato_id": str(contrato.id)},
        )

    assert result.tipo == tipo


@pytest.mark.asyncio
async def test_importar_documento_non_contrato_tipo_does_not_auto_create_contrato(db) -> None:
    """tipo != 'contrato' must NEVER trigger the auto-create-contrato/obligaciones
    pipeline, even when contrato_id/cuenta_cobro_id are omitted.

    Uses tipo=seguridad_social (a cuenta-level, non-`_NIVEL_CONTRATO` type) rather
    than rpc/rut/etc: those are contract-level and — per F1 — now REQUIRE a
    resolvable contrato when neither id is given (see
    test_tool_catalog_importar_documento_scoping.py), so a bare call would raise
    ValidationError instead of exercising the auto-create guard this test targets.
    """
    user, _contrato = await _make_user_with_contrato(db, "noauto")
    attachment = ToolAttachment(
        filename="seguridad_social.txt", content_type="text/plain", data=b"contenido seg social"
    )
    ctx = ToolContext(db=db, usuario=user, attachments={"seguridad_social.txt": attachment})

    with patch(_PATCH_S3, return_value=_fake_storage()):
        result = await invoke_tool(
            "importar_documento",
            ctx,
            {"filename": "seguridad_social.txt", "tipo": "seguridad_social"},
        )

    assert result.tipo == "seguridad_social"
    assert result.contrato_id is None
