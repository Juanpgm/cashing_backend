"""Neutral `TipoDocumentoFuente.OTROS` for uploads with no declared document type.

Requisitos with no `tipo_documento_fuente` (EVIDENCIAS and every user-defined
per-cuenta requisito) had no representable enum value: the web client sends
`"otros"` and the mobile client sends the lowercased requisito code, both 422.
Falling back to `contrato` is not an option — that is the destructive default.

OTROS is deliberately inert: it must never auto-link to a checklist row and must
never be read as agent instructions.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.services.document_service import upload_document
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_OBLIGACIONES = "app.services.document_service._extraer_obligaciones"

_TEXTO_SUFICIENTE = "Soporte generico adjuntado por el contratista al checklist. " * 5
_PDF_MAGIC = b"%PDF-1.4\n"


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_contrato(db: AsyncSession, user_id: uuid.UUID) -> Contrato:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CD-OTROS-001",
        objeto="Objeto de prueba para el tipo neutro",
        valor_total=12_000_000.0,
        valor_mensual=1_000_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return contrato


async def _crear_cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cuenta = CuentaCobro(
        contrato_id=contrato.id,
        mes=1,
        anio=2025,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=1_000_000,
        requisitos_modo="estandar",
    )
    db.add(cuenta)
    await db.commit()
    await db.refresh(cuenta)
    return cuenta


class TestOtrosEnumMember:
    async def test_otros_is_a_valid_tipo(self) -> None:
        assert TipoDocumentoFuente("otros") is TipoDocumentoFuente.OTROS
        assert TipoDocumentoFuente.OTROS.value == "otros"

    async def test_otros_never_auto_links_to_a_checklist_requisito(self) -> None:
        from app.services.document_classifier import TIPO_A_REQUISITO

        assert TipoDocumentoFuente.OTROS.value not in TIPO_A_REQUISITO

    async def test_otros_does_not_count_as_agent_instructions(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.document_service import verificar_configuracion_contrato

        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        db.add(
            DocumentoFuente(
                usuario_id=user.id,
                contrato_id=contrato.id,
                cuenta_cobro_id=None,
                storage_key="k/otros.pdf",
                nombre="adjunto.pdf",
                tipo=TipoDocumentoFuente.OTROS,
                texto_extraido="Notas sueltas que no son directivas para el agente.",
            )
        )
        await db.commit()

        config = await verificar_configuracion_contrato(db, user.id, contrato.id)
        assert config.tiene_instrucciones is False

    async def test_otros_text_never_reaches_the_agent_directives_prompt(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        from app.services.contrato_service import obtener_contexto_agente

        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        db.add(
            DocumentoFuente(
                usuario_id=user.id,
                contrato_id=contrato.id,
                cuenta_cobro_id=None,
                storage_key="k/otros.pdf",
                nombre="adjunto.pdf",
                tipo=TipoDocumentoFuente.OTROS,
                texto_extraido="TEXTO_QUE_NO_DEBE_LLEGAR_AL_PROMPT",
            )
        )
        await db.commit()

        contexto = await obtener_contexto_agente(db, user.id, contrato.id)
        assert "TEXTO_QUE_NO_DEBE_LLEGAR_AL_PROMPT" not in (contexto.system_prompt or "")
        assert contexto.instrucciones_usuario is None


class TestOtrosUploads:
    async def test_evidencias_requisito_upload_succeeds_with_otros(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """EVIDENCIAS declares no tipo_documento_fuente — the neutral tipo is the only
        representable value, and it must not touch the contract document."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)
        doc_contrato = DocumentoFuente(
            usuario_id=user.id,
            contrato_id=contrato.id,
            cuenta_cobro_id=None,
            storage_key="k/contrato.pdf",
            nombre="contrato.pdf",
            tipo=TipoDocumentoFuente.CONTRATO,
        )
        db.add(doc_contrato)
        await db.commit()
        await db.refresh(doc_contrato)

        with (
            patch(_PATCH_S3) as mock_storage_cls,
            patch(_PATCH_OBLIGACIONES, new=AsyncMock(return_value=([], []))),
        ):
            storage = _mock_storage()
            mock_storage_cls.return_value = storage

            resp = await upload_document(
                db=db,
                user_id=user.id,
                filename="evidencia-obligacion-1.txt",
                content=_TEXTO_SUFICIENTE.encode(),
                content_type="text/plain",
                tipo=TipoDocumentoFuente.OTROS,
                cuenta_cobro_id=cuenta.id,
                requisito_codigo="EVIDENCIAS",
            )

        assert resp.tipo == "otros"
        storage.delete.assert_not_called()
        assert await db.get(DocumentoFuente, doc_contrato.id) is not None
        fila = await db.get(DocumentoFuente, resp.id)
        assert fila is not None and fila.cuenta_cobro_id == cuenta.id

    async def test_api_accepts_tipo_otros_query_param(
        self, client: AsyncClient, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """The web client already sends tipo=otros; today that is a 422."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)
        cuenta = await _crear_cuenta(db, contrato)

        with patch(_PATCH_S3) as mock_storage_cls:
            mock_storage_cls.return_value = _mock_storage()
            r = await client.post(
                "/api/v1/documentos/upload",
                headers=test_user["headers"],
                params={
                    "tipo": "otros",
                    "cuenta_cobro_id": str(cuenta.id),
                    "requisito_codigo": "EVIDENCIAS",
                },
                files={"file": ("evidencia.pdf", _PDF_MAGIC + b"evidencia", "application/pdf")},
            )

        assert r.status_code == 201, r.text
        assert r.json()["tipo"] == "otros"


class TestCatalogAgreesWithEnum:
    async def test_every_catalog_tipo_is_representable_by_the_enum(self) -> None:
        """Regression guard for the CDP class of bug: the checklist catalog must never
        declare a tipo_documento_fuente the enum cannot represent."""
        from app.services.checklist_service import _CATALOGO_SEED

        declarados = {item["tipo_documento_fuente"] for item in _CATALOGO_SEED}
        declarados.discard(None)
        valores_enum = {t.value for t in TipoDocumentoFuente}

        assert declarados <= valores_enum, f"catalog declares unrepresentable tipos: {declarados - valores_enum}"

    async def test_every_mapped_requisito_tipo_is_representable_by_the_enum(self) -> None:
        from app.services.document_classifier import TIPO_A_REQUISITO

        valores_enum = {t.value for t in TipoDocumentoFuente}
        assert set(TIPO_A_REQUISITO) <= valores_enum
