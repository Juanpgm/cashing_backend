"""Integration tests for the hybrid text→vision document extraction path."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.schemas.agent import (
    ContratoCamposLLM,
    ContratoExtractionResult,
    LLMResponse,
    ObligacionItemLLM,
)
from sqlalchemy.ext.asyncio import AsyncSession

_PATCH_GET_LLM = "app.adapters.llm.get_llm"
_PATCH_PARSE_PDF = "app.agent.tools.document_parser.parse_pdf"
_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_GET_GRAPH = "app.services.agent_service.get_graph"
_PATCH_MULTIMODAL = "app.services.document_service._extraer_contrato_multimodal"

# A realistic (>200 char) contract text — classified as "sufficient".
_LONG_CONTRACT_TEXT = (
    "CONTRATO DE PRESTACIÓN DE SERVICIOS No. TXT-001-2025 celebrado entre la entidad "
    "contratante y el contratista. OBJETO: prestación de servicios profesionales de "
    "desarrollo de software. VALOR TOTAL: doce millones de pesos. PLAZO: del 1 de enero "
    "al 30 de junio de 2025. OBLIGACIONES ESPECÍFICAS del contratista a continuación."
)


def _vision_result() -> ContratoExtractionResult:
    return ContratoExtractionResult(
        contrato=ContratoCamposLLM(
            numero_contrato="VIS-001-2025",
            objeto="Servicios extraídos por visión",
            valor_total="6000000.00",
            valor_mensual="1000000.00",
            fecha_inicio="2025-01-01",
            fecha_fin="2025-06-30",
            entidad="MinTIC",
        ),
        obligaciones=[
            ObligacionItemLLM(descripcion="Desarrollar el sistema de información", tipo="especifica"),
            ObligacionItemLLM(descripcion="Pagar seguridad social", tipo="general"),
        ],
        transcripcion="Texto transcrito por el modelo de visión del contrato escaneado.",
    )


class TestHybridMultimodalFallback:
    @pytest.mark.asyncio
    async def test_scanned_pdf_uses_vision_and_creates_contrato(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """A scanned PDF (no extractable text) routes to the vision path,
        creating the contract and persisting only its specific obligations."""
        from app.services.document_service import upload_document

        user = test_user["user"]

        with (
            patch(_PATCH_MULTIMODAL, new=AsyncMock(return_value=_vision_result())),
            patch(_PATCH_PARSE_PDF, return_value=""),  # scanned → no text
            patch(_PATCH_S3) as mock_storage_cls,
        ):
            mock_storage = AsyncMock()
            mock_storage.upload = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato_escaneado.pdf",
                content=b"%PDF-1.4 scanned image only",
                content_type="application/pdf",
                tipo="contrato",
                contrato_id=None,
            )

        assert result.contrato_id is not None
        assert result.contrato_creado is not None
        assert result.contrato_creado.numero_contrato == "VIS-001-2025"
        assert result.texto_extraido == "Texto transcrito por el modelo de visión del contrato escaneado."
        assert len(result.obligaciones_extraidas) == 1
        assert result.obligaciones_extraidas[0].tipo == "especifica"

    @pytest.mark.asyncio
    async def test_image_upload_routes_to_vision(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        """An image upload (parse_document has no text path for it) routes to vision."""
        from app.services.document_service import upload_document

        user = test_user["user"]

        with (
            patch(_PATCH_MULTIMODAL, new=AsyncMock(return_value=_vision_result())) as mock_mm,
            patch(_PATCH_S3) as mock_storage_cls,
        ):
            mock_storage = AsyncMock()
            mock_storage.upload = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato.jpg",
                content=b"\xff\xd8\xff fake jpeg bytes",
                content_type="image/jpeg",
                tipo="contrato",
                contrato_id=None,
            )

        mock_mm.assert_awaited_once()
        assert result.contrato_id is not None
        assert result.contrato_creado is not None
        assert result.contrato_creado.numero_contrato == "VIS-001-2025"

    @pytest.mark.asyncio
    async def test_sufficient_text_skips_vision(self, db: AsyncSession, test_user: dict[str, Any]) -> None:
        """When text extraction is rich enough, the vision path is never invoked."""
        from app.services.document_service import upload_document

        user = test_user["user"]

        mock_multimodal = AsyncMock()
        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "contrato_extraido": {
                    "numero_contrato": "TXT-001-2025",
                    "objeto": "Prestación de servicios profesionales de desarrollo",
                    "valor_total": "12000000.00",
                    "valor_mensual": "2000000.00",
                    "fecha_inicio": "2025-01-01",
                    "fecha_fin": "2025-06-30",
                }
            }
        )
        llm_resp = LLMResponse(
            content="OBLIGACION|especifica|Desarrollar los módulos del sistema\n",
            model="test",
            total_tokens=20,
        )

        with (
            patch(_PATCH_MULTIMODAL, new=mock_multimodal),
            patch(_PATCH_GET_GRAPH, return_value=mock_graph),
            patch(_PATCH_GET_LLM) as mock_get_llm,
            patch(_PATCH_PARSE_PDF, return_value=_LONG_CONTRACT_TEXT),
            patch(_PATCH_S3) as mock_storage_cls,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_resp)
            mock_get_llm.return_value = mock_llm

            mock_storage = AsyncMock()
            mock_storage.upload = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato.pdf",
                content=b"%PDF-1.4 text contract",
                content_type="application/pdf",
                tipo="contrato",
                contrato_id=None,
            )

        mock_multimodal.assert_not_awaited()
        assert result.contrato_creado is not None
        assert result.contrato_creado.numero_contrato == "TXT-001-2025"
