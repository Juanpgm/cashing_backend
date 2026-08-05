"""Unit tests for document_service.py functions not covered by existing tests."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# _obtener_obligaciones_existentes
# ---------------------------------------------------------------------------


class TestObtenerObligacionesExistentes:
    @pytest.mark.asyncio
    async def test_returns_obligations_from_db(self) -> None:
        from app.services.document_service import _obtener_obligaciones_existentes

        ob = MagicMock()
        ob.descripcion = "Entregar informes mensuales"
        ob.tipo = MagicMock()
        ob.tipo.value = "entregable"
        ob.orden = 1

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [ob]

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        contrato_id = uuid.uuid4()
        result = await _obtener_obligaciones_existentes(contrato_id, mock_db)

        assert len(result) == 1
        assert result[0].descripcion == "Entregar informes mensuales"
        assert result[0].tipo == "entregable"
        assert result[0].orden == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_obligations(self) -> None:
        from app.services.document_service import _obtener_obligaciones_existentes

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = []

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        result = await _obtener_obligaciones_existentes(uuid.uuid4(), mock_db)
        assert result == []


# ---------------------------------------------------------------------------
# process_document
# ---------------------------------------------------------------------------


class TestProcessDocument:
    @pytest.mark.asyncio
    async def test_raises_not_found_when_doc_missing(self) -> None:
        from app.core.exceptions import NotFoundError
        from app.services.document_service import process_document

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        with pytest.raises(NotFoundError):
            await process_document(mock_db, uuid.uuid4(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_reprocesses_existing_document(self) -> None:
        from app.services.document_service import process_document

        doc = MagicMock()
        doc.id = uuid.uuid4()
        doc.storage_key = "usuarios/abc/documentos/file.pdf"
        doc.nombre = "contrato.pdf"
        doc.metadata_json = {}
        doc.texto_extraido = "old text"

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = doc

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        with patch("app.services.document_service._get_storage") as mock_s3_cls:
            mock_s3 = AsyncMock()
            mock_s3.download.return_value = b"%PDF-content"
            mock_s3_cls.return_value = mock_s3

            with patch(
                "app.services.document_service.parse_document",
                return_value="new extracted text",
            ):
                result = await process_document(mock_db, uuid.uuid4(), uuid.uuid4())

        assert result.texto_extraido == "new extracted text"
        assert doc.texto_extraido == "new extracted text"
        mock_db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# listar_documentos_contrato
# ---------------------------------------------------------------------------


class TestListarDocumentosContrato:
    @pytest.mark.asyncio
    async def test_raises_not_found_when_contrato_missing(self) -> None:
        from app.core.exceptions import NotFoundError
        from app.services.document_service import listar_documentos_contrato

        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = contrato_result

        with pytest.raises(NotFoundError):
            await listar_documentos_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_returns_documents_for_contract(self) -> None:
        from app.services.document_service import listar_documentos_contrato

        contrato = MagicMock()
        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = contrato

        from app.models.documento_fuente import TipoDocumentoFuente

        doc = MagicMock()
        doc.id = uuid.uuid4()
        doc.nombre = "contrato.pdf"
        doc.tipo = TipoDocumentoFuente.CONTRATO
        doc.contrato_id = uuid.uuid4()
        doc.texto_extraido = "some text"
        doc.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        docs_result = MagicMock()
        docs_result.scalars.return_value.all.return_value = [doc]

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contrato_result, docs_result]

        result = await listar_documentos_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

        assert len(result) == 1
        assert result[0].nombre == "contrato.pdf"
        assert result[0].tiene_texto is True


# ---------------------------------------------------------------------------
# verificar_configuracion_contrato
# ---------------------------------------------------------------------------


class TestVerificarConfiguracionContrato:
    def _make_contrato(self) -> MagicMock:
        c = MagicMock()
        c.id = uuid.uuid4()
        c.numero_contrato = "CON-001"
        c.objeto = "Prestacion de servicios"
        c.entidad = "Alcaldia"
        c.dependencia = "Sec. TIC"
        c.supervisor_nombre = "Supervisor"
        c.fecha_inicio = date(2024, 1, 1)
        c.fecha_fin = date(2024, 12, 31)
        c.valor_total = 12000000
        c.valor_mensual = 1000000
        c.obligaciones = []
        return c

    @pytest.mark.asyncio
    async def test_raises_not_found_when_contrato_missing(self) -> None:
        from app.core.exceptions import NotFoundError
        from app.services.document_service import verificar_configuracion_contrato

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.return_value = result_mock

        with pytest.raises(NotFoundError):
            await verificar_configuracion_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

    @pytest.mark.asyncio
    async def test_returns_not_ready_when_no_documents(self) -> None:
        from app.services.document_service import verificar_configuracion_contrato

        contrato = self._make_contrato()

        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = contrato

        docs_result = MagicMock()
        docs_result.scalars.return_value.all.return_value = []

        plantilla_result = MagicMock()
        plantilla_result.scalars.return_value.first.return_value = None

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contrato_result, docs_result, plantilla_result]

        result = await verificar_configuracion_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

        assert result.listo is False
        assert result.tiene_texto_contrato is False
        assert result.tiene_instrucciones is False
        assert len(result.faltantes) > 0

    @pytest.mark.asyncio
    async def test_returns_ready_when_all_present(self) -> None:
        from app.models.documento_fuente import TipoDocumentoFuente
        from app.services.document_service import verificar_configuracion_contrato

        contrato = self._make_contrato()

        # Add obligation
        ob = MagicMock()
        ob.tipo = MagicMock()
        ob.tipo.value = "entregable"
        ob.descripcion = "Entregar informes"
        ob.orden = 1
        contrato.obligaciones = [ob]

        contrato_result = MagicMock()
        contrato_result.scalar_one_or_none.return_value = contrato

        doc_contrato = MagicMock()
        doc_contrato.id = uuid.uuid4()
        doc_contrato.nombre = "contrato.pdf"
        doc_contrato.tipo = TipoDocumentoFuente.CONTRATO
        doc_contrato.contrato_id = contrato.id
        doc_contrato.texto_extraido = "Texto del contrato con obligaciones y clausulas"
        doc_contrato.created_at = datetime(2024, 1, 1, tzinfo=UTC)

        doc_instrucciones = MagicMock()
        doc_instrucciones.id = uuid.uuid4()
        doc_instrucciones.nombre = "instrucciones.txt"
        doc_instrucciones.tipo = TipoDocumentoFuente.INSTRUCCIONES
        doc_instrucciones.contrato_id = contrato.id
        doc_instrucciones.texto_extraido = "Instrucciones de trabajo"
        doc_instrucciones.created_at = datetime(2024, 1, 2, tzinfo=UTC)

        docs_result = MagicMock()
        docs_result.scalars.return_value.all.return_value = [doc_contrato, doc_instrucciones]

        plantilla_result = MagicMock()
        plantilla_result.scalars.return_value.first.return_value = MagicMock()  # plantilla exists

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [contrato_result, docs_result, plantilla_result]

        result = await verificar_configuracion_contrato(mock_db, uuid.uuid4(), uuid.uuid4())

        assert result.listo is True
        assert result.tiene_texto_contrato is True
        assert result.tiene_instrucciones is True
        assert result.tiene_obligaciones is True
        assert result.faltantes == []


# ---------------------------------------------------------------------------
# extraer_texto_documento(relaxed_ocr=...)
#
# Real scanned evidence PDFs run through RapidOCR often recover readable text
# that is concatenated ("Consultalasactividadesprogramadas") — the strict
# `is_text_sufficient` spacing heuristic rejects it (by design, for the
# contrato/vision escalation path), which otherwise discards perfectly
# classifiable evidence text. `relaxed_ocr=True` accepts it anyway above a
# much lower char floor (`settings.EVIDENCE_MIN_TEXT_CHARS`).
# ---------------------------------------------------------------------------

# One long concatenated "word" repeated — over EXTRACTION_MIN_TEXT_CHARS (200)
# but every token exceeds 20 chars, so is_text_sufficient's spacing heuristic
# rejects it regardless of the char-count gate.
_CONCATENATED_OCR_TEXT = "Consultalasactividadesprogramadasenelmesdemarzoparaelseguimientodelcontrato " * 4


class TestExtraerTextoDocumentoRelaxedOcr:
    @pytest.mark.asyncio
    async def test_relaxed_true_accepts_concatenated_ocr_text_above_floor(self) -> None:
        from app.services.document_service import extraer_texto_documento

        with (
            # OCR is opt-in (off by default); enable it to exercise the OCR tier.
            patch("app.services.document_service.settings.EXTRACTION_OCR_ENABLED", True),
            patch("app.services.document_service.parse_document", return_value=None),
            patch("app.services.document_service.ocr_available", return_value=True),
            patch(
                "app.services.document_service.ocr_extract_text",
                return_value=_CONCATENATED_OCR_TEXT,
            ),
        ):
            texto, avisos = await extraer_texto_documento(b"%PDF-fake", "escaneo.pdf", relaxed_ocr=True)

        assert texto == _CONCATENATED_OCR_TEXT.strip()
        assert any("espaciado imperfecto" in a for a in avisos)

    @pytest.mark.asyncio
    async def test_relaxed_true_still_rejects_text_below_floor(self) -> None:
        from app.services.document_service import extraer_texto_documento

        with (
            # OCR is opt-in (off by default); enable it to exercise the OCR tier.
            patch("app.services.document_service.settings.EXTRACTION_OCR_ENABLED", True),
            patch("app.services.document_service.parse_document", return_value=None),
            patch("app.services.document_service.ocr_available", return_value=True),
            patch("app.services.document_service.ocr_extract_text", return_value="Pago"),
        ):
            texto, avisos = await extraer_texto_documento(b"%PDF-fake", "escaneo.pdf", relaxed_ocr=True)

        assert texto is None
        assert any("No se pudo extraer texto legible" in a for a in avisos)

    @pytest.mark.asyncio
    async def test_relaxed_false_default_keeps_current_behavior(self) -> None:
        from app.services.document_service import extraer_texto_documento

        with (
            patch("app.services.document_service.parse_document", return_value=None),
            patch("app.services.document_service.ocr_available", return_value=True),
            patch(
                "app.services.document_service.ocr_extract_text",
                return_value=_CONCATENATED_OCR_TEXT,
            ),
        ):
            texto, avisos = await extraer_texto_documento(b"%PDF-fake", "escaneo.pdf")

        assert texto is None
        assert any("No se pudo extraer texto legible" in a for a in avisos)
        assert not any("espaciado imperfecto" in a for a in avisos)


# ---------------------------------------------------------------------------
# extraer_texto_documento — content-sniffed MIME reaches the OCR tier
#
# Root cause of the "OCR silently skipped" bug: the ladder used to gate the
# OCR tier on `guess_mime_type(filename)` (extension-only), which resolves an
# unextensioned SECOP document to octet-stream and skips OCR entirely even
# though the bytes are a perfectly OCR-able PDF.
# ---------------------------------------------------------------------------


class TestExtraerTextoDocumentoContentSniff:
    @pytest.mark.asyncio
    async def test_ocr_tier_reached_for_unextensioned_pdf_bytes(self) -> None:
        from app.services.document_service import extraer_texto_documento

        ocr_calls: list[tuple[bytes, str]] = []

        def _fake_ocr(content: bytes, mime: str, **kwargs: object) -> str:
            ocr_calls.append((content, mime))
            return "obligacion contractual del contratista " * 6  # >= EXTRACTION_MIN_TEXT_CHARS, well-spaced

        with (
            # OCR is opt-in (off by default); enable it to exercise the OCR tier.
            patch("app.services.document_service.settings.EXTRACTION_OCR_ENABLED", True),
            patch("app.services.document_service.parse_document", return_value=None),
            patch("app.services.document_service.ocr_available", return_value=True),
            patch("app.services.document_service.ocr_extract_text", side_effect=_fake_ocr),
        ):
            texto, avisos = await extraer_texto_documento(b"%PDF-1.4 escaneado", "documento_sin_extension")

        assert ocr_calls == [(b"%PDF-1.4 escaneado", "application/pdf")]
        assert texto is not None and texto.strip()
        assert avisos == []

    @pytest.mark.asyncio
    async def test_ocr_tier_still_skipped_for_genuinely_unrecognized_content(self) -> None:
        """Content that sniffs to nothing recognizable keeps the prior behavior:
        octet-stream, OCR tier skipped."""
        from app.services.document_service import extraer_texto_documento

        with (
            patch("app.services.document_service.parse_document", return_value=None),
            patch("app.services.document_service.ocr_available", return_value=True),
            patch("app.services.document_service.ocr_extract_text") as mock_ocr,
        ):
            texto, avisos = await extraer_texto_documento(b"garbage bytes", "documento_sin_extension")

        mock_ocr.assert_not_called()
        assert texto is None
        assert any("No se pudo extraer texto legible" in a for a in avisos)

    @pytest.mark.asyncio
    async def test_ocr_tier_disabled_by_default_scan_stays_native_only(self) -> None:
        """OCR is opt-in (EXTRACTION_OCR_ENABLED defaults to False): a scanned PDF
        with no native text must NOT invoke the local OCR engine — the ladder is
        native → vision, with vision reserved for the contract-extraction paths.
        On-premise/local hosts re-enable OCR by setting EXTRACTION_OCR_ENABLED=true."""
        from app.core.config import settings
        from app.services.document_service import extraer_texto_documento

        assert settings.EXTRACTION_OCR_ENABLED is False  # default contract

        with (
            patch("app.services.document_service.parse_document", return_value=None),
            patch("app.services.document_service.ocr_available", return_value=True),
            patch("app.services.document_service.ocr_extract_text") as mock_ocr,
        ):
            texto, avisos = await extraer_texto_documento(b"%PDF-1.4 escaneado", "escaneo.pdf")

        mock_ocr.assert_not_called()
        assert texto is None
        assert any("No se pudo extraer texto legible" in a for a in avisos)


# ---------------------------------------------------------------------------
# transcribir_documento_multimodal — plain-text vision fallback resilience
# ---------------------------------------------------------------------------


class TestTranscribirDocumentoMultimodal:
    @pytest.mark.asyncio
    async def test_unsupported_mime_returns_none_without_calling_any_model(self) -> None:
        from app.services.document_service import transcribir_documento_multimodal

        with patch("app.adapters.llm.get_llm") as mock_get_llm:
            resultado = await transcribir_documento_multimodal(b"hola", "text/plain")

        assert resultado is None
        mock_get_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_first_model_fails_second_recovers_text(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.schemas.agent import LLMResponse
        from app.services import document_service

        monkeypatch.setattr(document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-flash-latest")
        monkeypatch.setattr(document_service.settings, "GEMINI_API_KEY", "g")
        monkeypatch.setattr(document_service.settings, "GROQ_API_KEY", "k")

        class _FakeLLM:
            def __init__(self, model: str) -> None:
                self._model = model

            async def complete(self, messages: list[object], **kwargs: object) -> LLMResponse:
                if self._model == "gemini/gemini-flash-latest":
                    raise RuntimeError("provider down")
                return LLMResponse(content="texto transcrito por el segundo modelo", model=self._model)

        import app.adapters.llm as llm_module

        monkeypatch.setattr(llm_module, "get_llm", lambda model=None: _FakeLLM(model))

        png = b"\x89PNG\r\n\x1a\n fake but content is ignored by the fake LLM"
        resultado = await document_service.transcribir_documento_multimodal(png, "image/png")

        assert resultado == "texto transcrito por el segundo modelo"

    @pytest.mark.asyncio
    async def test_all_models_fail_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.services import document_service

        monkeypatch.setattr(document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-flash-latest")
        monkeypatch.setattr(document_service.settings, "GEMINI_API_KEY", "g")
        monkeypatch.setattr(document_service.settings, "GROQ_API_KEY", "k")

        class _AlwaysFailsLLM:
            def __init__(self, model: str) -> None: ...

            async def complete(self, messages: list[object], **kwargs: object) -> None:
                raise RuntimeError("provider down")

        import app.adapters.llm as llm_module

        monkeypatch.setattr(llm_module, "get_llm", lambda model=None: _AlwaysFailsLLM(model))

        png = b"\x89PNG\r\n\x1a\n fake"
        resultado = await document_service.transcribir_documento_multimodal(png, "image/png")

        assert resultado is None

    @pytest.mark.asyncio
    async def test_empty_response_is_treated_as_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.schemas.agent import LLMResponse
        from app.services import document_service

        monkeypatch.setattr(document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-flash-latest")
        monkeypatch.setattr(document_service.settings, "GEMINI_API_KEY", "g")
        monkeypatch.setattr(document_service.settings, "GROQ_API_KEY", "")

        class _EmptyLLM:
            def __init__(self, model: str) -> None: ...

            async def complete(self, messages: list[object], **kwargs: object) -> LLMResponse:
                return LLMResponse(content="   ", model="gemini/gemini-flash-latest")

        import app.adapters.llm as llm_module

        monkeypatch.setattr(llm_module, "get_llm", lambda model=None: _EmptyLLM(model))

        png = b"\x89PNG\r\n\x1a\n fake"
        resultado = await document_service.transcribir_documento_multimodal(png, "image/png")

        assert resultado is None


# ---------------------------------------------------------------------------
# _extraer_contrato_multimodal — raw model output must not leak into logs
# ---------------------------------------------------------------------------


class TestMultimodalParseFailedDoesNotLogRaw:
    @pytest.mark.asyncio
    async def test_parse_failure_logs_length_not_raw_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A response that fails schema validation logs the failure for
        diagnosis, but must NOT include the raw model output (can contain
        contract PII: cédula, dirección, valores) -- only its length."""
        from app.schemas.agent import LLMResponse
        from app.services import document_service

        monkeypatch.setattr(document_service.settings, "LLM_MULTIMODAL_MODEL", "gemini/gemini-flash-latest")
        monkeypatch.setattr(document_service.settings, "GEMINI_API_KEY", "g")
        monkeypatch.setattr(document_service.settings, "GROQ_API_KEY", "")

        raw_pii_content = "not valid json but leaks cédula 123456789, dirección Calle Falsa 123"

        class _InvalidJsonLLM:
            def __init__(self, model: str) -> None: ...

            async def complete(self, messages: list[object], **kwargs: object) -> LLMResponse:
                return LLMResponse(content=raw_pii_content, model="gemini/gemini-flash-latest")

        import app.adapters.llm as llm_module

        monkeypatch.setattr(llm_module, "get_llm", lambda model=None: _InvalidJsonLLM(model))

        logged: list[tuple[str, dict]] = []

        async def _fake_awarning(event: str, **kwargs):
            logged.append((event, kwargs))

        monkeypatch.setattr(document_service.logger, "awarning", _fake_awarning)

        png = b"\x89PNG\r\n\x1a\n fake"
        result = await document_service._extraer_contrato_multimodal(png, "image/png")

        assert result is None
        parse_failed_calls = [kwargs for event, kwargs in logged if event == "multimodal_parse_failed"]
        assert len(parse_failed_calls) == 1
        kwargs = parse_failed_calls[0]
        assert "raw" not in kwargs
        assert "raw_response" not in kwargs
        assert kwargs.get("raw_len") == len(raw_pii_content)
        assert "cédula" not in str(kwargs)
