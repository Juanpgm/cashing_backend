"""Integration tests for the `plantillas_organismo` tools (billing-resilience-
templates, slice #5, task 5.13): tool registration + `invoke_tool` read/write
behavior. Mirrors `tests/test_adicion_contrato_service.py`'s structure.

Extraction/persistence unit tests live in
`tests/services/test_plantilla_organismo_ingestion.py`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import app.tools.catalog  # noqa: F401 — import-for-side-effect: registers every catalog tool
import pytest
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.services import document_service
from app.tools.catalog.plantillas_organismo import (
    IngerirPlantillaOrganismoInput,
    ObtenerPlantillaOrganismoInput,
)
from app.tools.context import ToolContext
from app.tools.invoke import invoke_tool
from app.tools.registry import TOOL_REGISTRY
from sqlalchemy.ext.asyncio import AsyncSession

_DAGMA_JSON = '{"columnas": ["obligación", "avance del periodo"], "secciones": [], "anexo_refs": [], "notas": ""}'


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, messages, temperature=0.0, max_tokens=4096, response_format=None, **kwargs):
        from app.schemas.agent import LLMResponse

        return LLMResponse(content=self._content, model="fake/test-model", total_tokens=30)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(content), raising=True)


def _fake_storage(content: bytes) -> AsyncMock:
    storage = AsyncMock()
    storage.download = AsyncMock(return_value=content)
    return storage


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PLANTILLA-TOOL-001",
        objeto="Servicios profesionales para pruebas del tool de plantillas",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def documento(db: AsyncSession, contrato: Contrato) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=contrato.usuario_id,
        contrato_id=contrato.id,
        storage_key="documentos/plantilla-tool.docx",
        nombre="plantilla-tool.docx",
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


def test_ingerir_plantilla_organismo_is_registered_as_write_tool() -> None:
    assert "ingerir_plantilla_organismo" in TOOL_REGISTRY
    assert TOOL_REGISTRY["ingerir_plantilla_organismo"].tags == ("write",)


def test_obtener_plantilla_organismo_is_registered_as_read_tool() -> None:
    assert "obtener_plantilla_organismo" in TOOL_REGISTRY
    assert TOOL_REGISTRY["obtener_plantilla_organismo"].tags == ("read",)


async def test_invoke_ingerir_plantilla_organismo_persists_structure(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    test_user: dict[str, Any],
    contrato: Contrato,
    documento: DocumentoFuente,
) -> None:
    monkeypatch.setattr(document_service, "extraer_texto_documento", AsyncMock(return_value=("texto", [])))
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"docx-bytes"))
    _patch_llm(monkeypatch, _DAGMA_JSON)

    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool(
        "ingerir_plantilla_organismo",
        ctx,
        IngerirPlantillaOrganismoInput(contrato_id=contrato.id, documento_fuente_id=documento.id),
    )
    await db.commit()

    assert result.persistida is True
    assert result.plantilla is not None
    assert result.plantilla.entidad == "DAGMA"
    assert result.plantilla.estructura_json["columnas"] == ["obligación", "avance del periodo"]


async def test_invoke_ingerir_plantilla_organismo_degrades_without_raising(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    test_user: dict[str, Any],
    contrato: Contrato,
    documento: DocumentoFuente,
) -> None:
    monkeypatch.setattr(document_service, "extraer_texto_documento", AsyncMock(return_value=(None, ["sin texto"])))
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"scanned-bytes"))
    monkeypatch.setattr(document_service, "vision_model_chain", lambda: [])

    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool(
        "ingerir_plantilla_organismo",
        ctx,
        IngerirPlantillaOrganismoInput(contrato_id=contrato.id, documento_fuente_id=documento.id),
    )

    assert result.persistida is False
    assert result.plantilla is None
    assert result.avisos


async def test_invoke_obtener_plantilla_organismo_returns_not_found_when_never_ingested(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    ctx = ToolContext(db=db, usuario=test_user["user"])
    result = await invoke_tool(
        "obtener_plantilla_organismo", ctx, ObtenerPlantillaOrganismoInput(contrato_id=contrato.id)
    )
    assert result.encontrada is False
    assert result.plantilla is None


async def test_invoke_obtener_plantilla_organismo_returns_persisted_structure(
    db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    test_user: dict[str, Any],
    contrato: Contrato,
    documento: DocumentoFuente,
) -> None:
    monkeypatch.setattr(document_service, "extraer_texto_documento", AsyncMock(return_value=("texto", [])))
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"docx-bytes"))
    _patch_llm(monkeypatch, _DAGMA_JSON)

    ctx = ToolContext(db=db, usuario=test_user["user"])
    await invoke_tool(
        "ingerir_plantilla_organismo",
        ctx,
        IngerirPlantillaOrganismoInput(contrato_id=contrato.id, documento_fuente_id=documento.id),
    )
    await db.commit()

    result = await invoke_tool(
        "obtener_plantilla_organismo", ctx, ObtenerPlantillaOrganismoInput(contrato_id=contrato.id)
    )
    assert result.encontrada is True
    assert result.plantilla is not None
    assert result.plantilla.estructura_json["columnas"] == ["obligación", "avance del periodo"]


async def test_invoke_ingerir_plantilla_organismo_raises_not_found_for_missing_documento(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    from app.core.exceptions import NotFoundError

    ctx = ToolContext(db=db, usuario=test_user["user"])
    with pytest.raises(NotFoundError):
        await invoke_tool(
            "ingerir_plantilla_organismo",
            ctx,
            IngerirPlantillaOrganismoInput(contrato_id=contrato.id, documento_fuente_id=uuid.uuid4()),
        )
