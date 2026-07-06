"""Tests for auto-creation of contracts from document upload."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from app.schemas.agent import ContratoExtraido, LLMResponse
from sqlalchemy.ext.asyncio import AsyncSession

# Patch path for get_llm — lazily imported inside service functions
_PATCH_GET_LLM = "app.adapters.llm.get_llm"
_PATCH_PARSE_PDF = "app.agent.tools.document_parser.parse_pdf"
_PATCH_S3 = "app.services.document_service._get_storage"
_PATCH_GET_GRAPH = "app.services.agent_service.get_graph"


# ── _parse_campos_llm unit tests ────────────────────────────────────


class TestParseCamposLLM:
    def test_parse_valid_output(self) -> None:
        from app.agent.tools.contract_parser import parse_campos_llm as _parse_campos_llm

        llm_output = (
            "CAMPO|numero_contrato|CD-045-2025\n"
            "CAMPO|objeto|Prestación de servicios profesionales\n"
            "CAMPO|valor_total|12000000.00\n"
            "CAMPO|valor_mensual|2000000.00\n"
            "CAMPO|fecha_inicio|2025-01-15\n"
            "CAMPO|fecha_fin|2025-07-14\n"
            "CAMPO|supervisor_nombre|María García López\n"
            "CAMPO|entidad|Ministerio de TIC\n"
            "CAMPO|dependencia|Dirección Digital\n"
            "CAMPO|documento_proveedor|1016019452\n"
        )
        result = _parse_campos_llm(llm_output)
        assert result["numero_contrato"] == "CD-045-2025"
        assert result["objeto"] == "Prestación de servicios profesionales"
        assert result["valor_total"] == "12000000.00"
        assert result["fecha_inicio"] == "2025-01-15"
        assert result["supervisor_nombre"] == "María García López"
        assert result["documento_proveedor"] == "1016019452"
        assert len(result) == 10

    def test_parse_partial_output(self) -> None:
        from app.agent.tools.contract_parser import parse_campos_llm as _parse_campos_llm

        llm_output = "CAMPO|numero_contrato|CPS-123-2024\nCAMPO|objeto|Consultoría técnica\n"
        result = _parse_campos_llm(llm_output)
        assert len(result) == 2
        assert result["numero_contrato"] == "CPS-123-2024"

    def test_parse_ignores_invalid_fields(self) -> None:
        from app.agent.tools.contract_parser import parse_campos_llm as _parse_campos_llm

        llm_output = "CAMPO|numero_contrato|CD-001\nCAMPO|campo_invalido|algo\ntexto extra que no es campo\n"
        result = _parse_campos_llm(llm_output)
        assert len(result) == 1
        assert "campo_invalido" not in result

    def test_parse_empty_output(self) -> None:
        from app.agent.tools.contract_parser import parse_campos_llm as _parse_campos_llm

        result = _parse_campos_llm("")
        assert result == {}

    def test_parse_markdown_bold_campo(self) -> None:
        from app.agent.tools.contract_parser import parse_campos_llm as _parse_campos_llm

        llm_output = "**CAMPO**|numero_contrato|CD-045-2025\n"
        result = _parse_campos_llm(llm_output)
        assert result["numero_contrato"] == "CD-045-2025"

    def test_parse_new_location_and_supervisor_fields(self) -> None:
        from app.agent.tools.contract_parser import parse_campos_llm as _parse_campos_llm

        llm_output = (
            "CAMPO|pais|Colombia\n"
            "CAMPO|departamento|Antioquia\n"
            "CAMPO|ciudad|Medellín\n"
            "CAMPO|direccion_ejecucion|Calle 44 # 52-165, Alpujarra\n"
            "CAMPO|cargo_supervisor|Director de TIC\n"
        )
        result = _parse_campos_llm(llm_output)
        assert result["pais"] == "Colombia"
        assert result["departamento"] == "Antioquia"
        assert result["ciudad"] == "Medellín"
        assert result["direccion_ejecucion"] == "Calle 44 # 52-165, Alpujarra"
        assert result["cargo_supervisor"] == "Director de TIC"


# ── _parse_obligaciones_llm unit tests ──────────────────────────────


class TestParseObligacionesLLM:
    def test_parse_standard_format(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = (
            "OBLIGACION|especifica|Diseñar los módulos del sistema\n"
            "OBLIGACION|general|Cumplir con el pago de seguridad social\n"
        )
        result = _parse_obligaciones_llm(output)
        # Only 'especifica' obligations are kept; 'general' are filtered out
        assert len(result) == 1
        assert result[0].tipo == "especifica"
        assert result[0].descripcion == "Diseñar los módulos del sistema"

    def test_parse_accented_obligacion(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = (
            "OBLIGACIÓN|específica|Diseñar los módulos del sistema\n"
            "OBLIGACIÓN|general|Cumplir con el pago de seguridad social\n"
        )
        result = _parse_obligaciones_llm(output)
        # Only 'especifica' kept
        assert len(result) == 1
        assert result[0].tipo == "especifica"
        assert result[0].descripcion == "Diseñar los módulos del sistema"

    def test_parse_accented_especifica_normalized(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = "OBLIGACION|específica|Presentar informes mensuales\n"
        result = _parse_obligaciones_llm(output)
        assert len(result) == 1
        assert result[0].tipo == "especifica"

    def test_parse_numbered_lines(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = "1. OBLIGACION|especifica|Diseñar módulos\n2. OBLIGACION|general|Pagar seguridad social\n"
        result = _parse_obligaciones_llm(output)
        assert len(result) == 1
        assert result[0].tipo == "especifica"

    def test_parse_bulleted_lines(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = "- OBLIGACION|especifica|Diseñar módulos\n* OBLIGACION|general|Pagar seguridad social\n"
        result = _parse_obligaciones_llm(output)
        assert len(result) == 1
        assert result[0].tipo == "especifica"

    def test_parse_markdown_bold(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = "**OBLIGACION**|especifica|Diseñar módulos del sistema\n"
        result = _parse_obligaciones_llm(output)
        assert len(result) == 1

    def test_parse_code_fenced_response(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = "```\nOBLIGACION|especifica|Diseñar módulos\nOBLIGACION|general|Pagar seguridad social\n```\n"
        result = _parse_obligaciones_llm(output)
        assert len(result) == 1
        assert result[0].tipo == "especifica"

    def test_parse_empty_returns_empty(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        assert _parse_obligaciones_llm("") == []
        assert _parse_obligaciones_llm("No se encontraron obligaciones.") == []

    def test_parse_mixed_accented_and_numbered(self) -> None:
        from app.agent.tools.contract_parser import parse_obligaciones_llm as _parse_obligaciones_llm

        output = (
            "1. OBLIGACIÓN|específica|Diseñar e implementar los módulos del sistema\n"
            "2) **OBLIGACIÓN**|general|Cumplir con el pago de aportes de seguridad social\n"
            "- OBLIGACION|especifica|Presentar informe mensual de actividades\n"
        )
        result = _parse_obligaciones_llm(output)
        # Only 2 'especifica' lines kept; 1 'general' filtered out
        assert len(result) == 2
        assert all(r.tipo == "especifica" for r in result)


# ── _extraer_datos_contrato unit tests ──────────────────────────────


class TestExtraerDatosContrato:
    @pytest.mark.asyncio
    async def test_extraction_success(self) -> None:
        from app.services.document_service import _extraer_datos_contrato

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "contrato_extraido": {
                    "numero_contrato": "CD-045-2025",
                    "objeto": "Prestación de servicios profesionales",
                    "valor_total": "12000000.00",
                    "valor_mensual": "2000000.00",
                    "fecha_inicio": "2025-01-15",
                    "fecha_fin": "2025-07-14",
                    "supervisor_nombre": "María García",
                    "entidad": "MinTIC",
                }
            }
        )

        with patch(_PATCH_GET_GRAPH, return_value=mock_graph):
            result, _avisos = await _extraer_datos_contrato("Texto del contrato de prueba...")
            assert result is not None
            assert result.numero_contrato == "CD-045-2025"
            assert result.objeto == "Prestación de servicios profesionales"
            assert result.valor_total == Decimal("12000000.00")
            assert result.valor_mensual == Decimal("2000000.00")
            assert result.fecha_inicio == date(2025, 1, 15)
            assert result.fecha_fin == date(2025, 7, 14)
            assert result.supervisor_nombre == "María García"
            assert result.entidad == "MinTIC"

    @pytest.mark.asyncio
    async def test_extraction_llm_failure(self) -> None:
        from app.services.document_service import _extraer_datos_contrato

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(side_effect=RuntimeError("LLM error"))

        with patch(_PATCH_GET_GRAPH, return_value=mock_graph):
            result, _avisos = await _extraer_datos_contrato("Texto del contrato...")
            assert result is None

    @pytest.mark.asyncio
    async def test_extraction_insufficient_data(self) -> None:
        from app.services.document_service import _extraer_datos_contrato

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(return_value={"contrato_extraido": {"supervisor_nombre": "María García"}})

        with patch(_PATCH_GET_GRAPH, return_value=mock_graph):
            result, _avisos = await _extraer_datos_contrato("Texto sin datos útiles...")
            assert result is None

    @pytest.mark.asyncio
    async def test_extraction_colombian_number_format(self) -> None:
        from app.services.document_service import _extraer_datos_contrato

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "contrato_extraido": {
                    "numero_contrato": "CD-001",
                    "objeto": "Consultoría",
                    "valor_total": "12.000.000,50",
                    "valor_mensual": "2.000.000,00",
                }
            }
        )

        with patch(_PATCH_GET_GRAPH, return_value=mock_graph):
            result, _avisos = await _extraer_datos_contrato("Texto del contrato...")
            assert result is not None
            assert result.valor_total == Decimal("12000000.50")
            assert result.valor_mensual == Decimal("2000000.00")


# ── upload_document auto-create integration tests ───────────────────


class TestUploadDocumentAutoCreate:
    @pytest.mark.asyncio
    async def test_upload_contract_without_contrato_id_creates_contrato(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
    ) -> None:
        """When uploading tipo=contrato without contrato_id, the system should
        auto-create a Contrato record with LLM-extracted data."""
        from app.services.document_service import upload_document

        user = test_user["user"]

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "contrato_extraido": {
                    "numero_contrato": "CD-099-2025",
                    "objeto": "Prestación de servicios profesionales de desarrollo",
                    "valor_total": "18000000.00",
                    "valor_mensual": "3000000.00",
                    "fecha_inicio": "2025-02-01",
                    "fecha_fin": "2025-07-31",
                    "entidad": "MinTIC",
                }
            }
        )

        llm_obligaciones_response = LLMResponse(
            content=(
                "OBLIGACION|especifica|Desarrollar módulos del sistema de información\n"
                "OBLIGACION|especifica|Realizar pruebas de integración del sistema\n"
            ),
            model="test",
            total_tokens=80,
        )

        with (
            patch(_PATCH_GET_GRAPH, return_value=mock_graph),
            patch(_PATCH_GET_LLM) as mock_get_llm,
            patch(
                _PATCH_PARSE_PDF,
                return_value=(
                    "CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES No. CD-099-2025 "
                    "celebrado entre el Ministerio de TIC y el contratista para el desarrollo "
                    "de módulos de software. OBJETO: prestación de servicios profesionales de "
                    "desarrollo. VALOR TOTAL: dieciocho millones de pesos. PLAZO: del 1 de "
                    "febrero al 31 de julio de 2025. OBLIGACIONES ESPECÍFICAS del contratista."
                ),
            ),
            patch(_PATCH_S3) as mock_storage_cls,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_obligaciones_response)
            mock_get_llm.return_value = mock_llm

            mock_storage = AsyncMock()
            mock_storage.upload = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato.pdf",
                content=b"fake pdf content",
                content_type="application/pdf",
                tipo="contrato",
                contrato_id=None,
            )

        assert result.contrato_id is not None
        assert result.contrato_creado is not None
        assert result.contrato_creado.numero_contrato == "CD-099-2025"
        assert result.contrato_creado.entidad == "MinTIC"
        assert len(result.obligaciones_extraidas) == 2
        assert all(o.tipo == "especifica" for o in result.obligaciones_extraidas)

    @pytest.mark.asyncio
    async def test_upload_contract_auto_creates_with_location_fields(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
    ) -> None:
        """Auto-created contract should persist location and cargo_supervisor fields."""
        from app.services.document_service import upload_document

        user = test_user["user"]

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "contrato_extraido": {
                    "numero_contrato": "LOC-001-2025",
                    "objeto": "Consultoría técnica especializada",
                    "valor_total": "6000000.00",
                    "valor_mensual": "1000000.00",
                    "fecha_inicio": "2025-01-01",
                    "fecha_fin": "2025-06-30",
                    "supervisor_nombre": "Juan Pérez",
                    "cargo_supervisor": "Director de TIC",
                    "entidad": "Alcaldía Municipal",
                    "pais": "Colombia",
                    "departamento": "Antioquia",
                    "ciudad": "Medellín",
                    "direccion_ejecucion": "Calle 44 # 52-165, Alpujarra",
                }
            }
        )

        llm_obligaciones_response = LLMResponse(
            content="OBLIGACION|especifica|Prestar consultoría técnica especializada\n",
            model="test",
            total_tokens=40,
        )

        with (
            patch(_PATCH_GET_GRAPH, return_value=mock_graph),
            patch(_PATCH_GET_LLM) as mock_get_llm,
            patch(
                _PATCH_PARSE_PDF,
                return_value=(
                    "CONTRATO DE CONSULTORÍA No. LOC-001-2025 celebrado entre la Alcaldía "
                    "Municipal de Medellín, Antioquia, y el contratista Juan Pérez. OBJETO: "
                    "consultoría técnica especializada. VALOR TOTAL: seis millones de pesos. "
                    "LUGAR DE EJECUCIÓN: Calle 44 # 52-165, Alpujarra, Medellín. PLAZO: del 1 "
                    "de enero al 30 de junio de 2025. SUPERVISOR: Director de TIC."
                ),
            ),
            patch(_PATCH_S3) as mock_storage_cls,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_obligaciones_response)
            mock_get_llm.return_value = mock_llm

            mock_storage = AsyncMock()
            mock_storage.upload = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato_medellin.pdf",
                content=b"fake pdf content",
                content_type="application/pdf",
                tipo="contrato",
                contrato_id=None,
            )

        assert result.contrato_creado is not None
        assert result.contrato_creado.cargo_supervisor == "Director de TIC"
        assert result.contrato_creado.pais == "Colombia"
        assert result.contrato_creado.departamento == "Antioquia"
        assert result.contrato_creado.ciudad == "Medellín"
        assert result.contrato_creado.direccion_ejecucion == "Calle 44 # 52-165, Alpujarra"

    @pytest.mark.asyncio
    async def test_upload_contract_defaults_documento_proveedor_from_cedula(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
    ) -> None:
        """When LLM doesn't extract documento_proveedor, it defaults to user cedula."""
        from app.models.contrato import Contrato
        from app.services.document_service import upload_document
        from sqlalchemy import select

        user = test_user["user"]
        user.cedula = "5555555555"
        await db.flush()

        mock_graph = AsyncMock()
        mock_graph.ainvoke = AsyncMock(
            return_value={
                "contrato_extraido": {
                    "numero_contrato": "CED-001-2025",
                    "objeto": "Prestación de servicios técnicos especializados",
                    "valor_total": "3000000.00",
                    "valor_mensual": "3000000.00",
                    "fecha_inicio": "2025-01-01",
                    "fecha_fin": "2025-01-31",
                    # No documento_proveedor — should default to user.cedula
                }
            }
        )

        llm_obligaciones_response = LLMResponse(
            content="",
            model="test",
            total_tokens=10,
        )

        with (
            patch(_PATCH_GET_GRAPH, return_value=mock_graph),
            patch(_PATCH_GET_LLM) as mock_get_llm,
            patch(
                _PATCH_PARSE_PDF,
                return_value=(
                    "CONTRATO DE PRESTACIÓN DE SERVICIOS No. CED-001-2025. OBJETO: prestación "
                    "de servicios técnicos especializados para la entidad contratante. VALOR "
                    "TOTAL: tres millones de pesos pagaderos en una sola cuota. PLAZO: del 1 al "
                    "31 de enero de 2025. El presente documento no registra el número de cédula "
                    "del contratista, por lo que deberá tomarse del perfil del usuario."
                ),
            ),
            patch(_PATCH_S3) as mock_storage_cls,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_obligaciones_response)
            mock_get_llm.return_value = mock_llm

            mock_storage = AsyncMock()
            mock_storage.upload = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato_sin_cedula.pdf",
                content=b"fake pdf content",
                content_type="application/pdf",
                tipo="contrato",
                contrato_id=None,
            )

        assert result.contrato_id is not None
        contrato_db_result = await db.execute(select(Contrato).where(Contrato.id == result.contrato_id))
        contrato_db = contrato_db_result.scalar_one()
        assert contrato_db.documento_proveedor == "5555555555"

    @pytest.mark.asyncio
    async def test_upload_contract_with_contrato_id_skips_auto_create(
        self,
        db: AsyncSession,
        test_user: dict[str, Any],
    ) -> None:
        """When contrato_id is provided, the existing flow should work without auto-creation."""
        from app.models.contrato import Contrato
        from app.services.document_service import upload_document

        user = test_user["user"]

        # Create an existing contract
        contrato = Contrato(
            usuario_id=user.id,
            numero_contrato="EXIST-001",
            objeto="Contrato existente para pruebas",
            valor_total=10000000.00,
            valor_mensual=2000000.00,
            fecha_inicio=date(2025, 1, 1),
            fecha_fin=date(2025, 6, 30),
        )
        db.add(contrato)
        await db.flush()

        llm_obligaciones_response = LLMResponse(
            content="OBLIGACION|especifica|Obligación de prueba\n",
            model="test",
            total_tokens=50,
        )

        with (
            patch(_PATCH_GET_LLM) as mock_get_llm,
            patch(_PATCH_PARSE_PDF, return_value="Texto del contrato"),
            patch(_PATCH_S3) as mock_storage_cls,
        ):
            mock_llm = AsyncMock()
            mock_llm.complete = AsyncMock(return_value=llm_obligaciones_response)
            mock_get_llm.return_value = mock_llm

            mock_storage = AsyncMock()
            mock_storage.upload = AsyncMock()
            mock_storage_cls.return_value = mock_storage

            result = await upload_document(
                db=db,
                user_id=user.id,
                filename="contrato_existente.pdf",
                content=b"fake pdf content",
                content_type="application/pdf",
                tipo="contrato",
                contrato_id=contrato.id,
            )

        assert result.contrato_id == contrato.id
        assert result.contrato_creado is None
        assert len(result.obligaciones_extraidas) == 1


# ── Document upload API integration tests ───────────────────────────


class TestDocumentUploadAPIAutoCreate:
    @pytest.mark.asyncio
    async def test_upload_api_returns_contrato_creado_field(
        self,
    ) -> None:
        """Verify the API layer passes through contrato_creado from service response."""
        from app.schemas.agent import DocumentUploadResponse

        mock_service_response = DocumentUploadResponse(
            id="00000000-0000-0000-0000-000000000001",
            nombre="contrato_test.pdf",
            tipo="contrato",
            texto_extraido="Texto contractual de prueba",
            contrato_id="00000000-0000-0000-0000-000000000002",
            contrato_creado=ContratoExtraido(
                numero_contrato="API-TEST-001",
                objeto="Servicios profesionales de consultoría",
                valor_total=Decimal("6000000.00"),
                valor_mensual=Decimal("1000000.00"),
                fecha_inicio=date(2025, 3, 1),
                fecha_fin=date(2025, 8, 31),
            ),
            obligaciones_extraidas=[],
        )

        # Verify schema serialization includes new fields
        data = mock_service_response.model_dump(mode="json")
        assert data["contrato_id"] == "00000000-0000-0000-0000-000000000002"
        assert data["contrato_creado"]["numero_contrato"] == "API-TEST-001"
        assert data["contrato_creado"]["valor_total"] == "6000000.00"
        assert data["contrato_creado"]["fecha_inicio"] == "2025-03-01"
        assert data["obligaciones_extraidas"] == []

    @pytest.mark.asyncio
    async def test_upload_api_no_contrato_created_has_null_fields(self) -> None:
        """Verify response without auto-creation has contrato_creado=None."""
        from app.schemas.agent import DocumentUploadResponse

        response = DocumentUploadResponse(
            id="00000000-0000-0000-0000-000000000001",
            nombre="instrucciones.docx",
            tipo="instrucciones",
            texto_extraido="Instrucciones del usuario",
        )
        data = response.model_dump(mode="json")
        assert data["contrato_id"] is None
        assert data["contrato_creado"] is None
