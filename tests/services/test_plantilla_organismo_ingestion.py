"""Tests for template-structure extraction and ingestion (billing-resilience-
templates, slice #5, tasks 5.7-5.12): `requisito_inference_service.
inferir_estructura_plantilla` / `ingerir_plantilla_organismo` /
`obtener_plantilla_organismo`.

Tool-wrapper tests (task 5.13) and packager-wiring tests (tasks 5.14-5.15) live
in `tests/test_plantilla_organismo_tool.py` and `tests/test_paquete_gate.py`
respectively.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest
from app.core.exceptions import ForbiddenError, NotFoundError, ValidationError
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.plantilla_organismo import PlantillaOrganismo
from app.schemas.agent import LLMResponse
from app.services import document_service
from app.services import requisito_inference_service as svc
from sqlalchemy.ext.asyncio import AsyncSession

_DAGMA_2COL_JSON = json.dumps(
    {
        "columnas": ["obligación", "avance del periodo"],
        "secciones": [],
        "anexo_refs": [],
        "notas": "Formato DAGMA — dos columnas, sin columna de evidencia.",
    }
)

_COEMPRESAR_3COL_JSON = json.dumps(
    {
        "columnas": ["obligación", "avance del periodo", "evidencia"],
        "secciones": [],
        "anexo_refs": ["Ver Anexo: Carpeta /5. EVIDENCIAS/A1"],
        "notas": "Formato COEMPRESAR — tres columnas con referencia a anexo.",
    }
)


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(self, messages, temperature=0.0, max_tokens=4096, response_format=None, **kwargs) -> LLMResponse:
        return LLMResponse(content=self._content, model="fake/test-model", total_tokens=30)


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(content), raising=True)


def _fake_storage(content: bytes) -> AsyncMock:
    storage = AsyncMock()
    storage.download = AsyncMock(return_value=content)
    return storage


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato_dagma(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PLANTILLA-DAGMA-001",
        objeto="Servicios profesionales — organismo DAGMA",
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
async def documento_dagma(db: AsyncSession, contrato_dagma: Contrato) -> DocumentoFuente:
    doc = DocumentoFuente(
        usuario_id=contrato_dagma.usuario_id,
        contrato_id=contrato_dagma.id,
        storage_key="documentos/plantilla-dagma.docx",
        nombre="plantilla-dagma.docx",
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


# ── 5.7 / Anexo verbatim — successful extraction ──────────────────────────────


async def test_docx_extraction_persists_dagma_2col_structure(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, contrato_dagma: Contrato, documento_dagma: DocumentoFuente
) -> None:
    monkeypatch.setattr(
        document_service, "extraer_texto_documento", AsyncMock(return_value=("texto plantilla DAGMA", []))
    )
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"docx-bytes"))
    _patch_llm(monkeypatch, _DAGMA_2COL_JSON)

    plantilla, avisos = await svc.ingerir_plantilla_organismo(
        db, contrato_dagma.usuario_id, contrato_dagma.id, documento_dagma.id
    )
    await db.commit()

    assert plantilla is not None
    assert avisos == []
    assert plantilla.entidad == "DAGMA"
    assert plantilla.entidad_normalizada == "dagma"
    assert plantilla.estructura_json["columnas"] == ["obligación", "avance del periodo"]
    assert plantilla.estructura_json["anexo_refs"] == []


async def test_anexo_reference_preserved_verbatim_coempresar_3col(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, test_user: dict[str, Any]
) -> None:
    user = test_user["user"]
    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PLANTILLA-COEMPRESAR-001",
        objeto="Servicios profesionales — organismo COEMPRESAR",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="COEMPRESAR",
        dependencia="Sistemas",
        supervisor_nombre="Sup",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="documentos/plantilla-coempresar.docx",
        nombre="plantilla-coempresar.docx",
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    monkeypatch.setattr(
        document_service, "extraer_texto_documento", AsyncMock(return_value=("texto plantilla COEMPRESAR", []))
    )
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"docx-bytes"))
    _patch_llm(monkeypatch, _COEMPRESAR_3COL_JSON)

    plantilla, avisos = await svc.ingerir_plantilla_organismo(db, user.id, contrato.id, doc.id)
    await db.commit()

    assert plantilla is not None
    assert avisos == []
    assert plantilla.estructura_json["columnas"] == ["obligación", "avance del periodo", "evidencia"]
    assert plantilla.estructura_json["anexo_refs"] == ["Ver Anexo: Carpeta /5. EVIDENCIAS/A1"]


# ── 5.9 — graceful degradation ────────────────────────────────────────────────


async def test_unreadable_template_degrades_safely_no_structure_persisted(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, contrato_dagma: Contrato, documento_dagma: DocumentoFuente
) -> None:
    monkeypatch.setattr(document_service, "extraer_texto_documento", AsyncMock(return_value=(None, ["sin texto"])))
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"scanned-bytes"))
    # No usable vision model (no credentials configured) — the vision fallback
    # chain itself degrades to empty, so extraction returns None end-to-end.
    monkeypatch.setattr(document_service, "vision_model_chain", lambda: [])

    plantilla, avisos = await svc.ingerir_plantilla_organismo(
        db, contrato_dagma.usuario_id, contrato_dagma.id, documento_dagma.id
    )
    await db.commit()

    assert plantilla is None
    assert avisos

    result = (
        (
            await db.execute(
                __import__("sqlalchemy")
                .select(PlantillaOrganismo)
                .where(PlantillaOrganismo.usuario_id == contrato_dagma.usuario_id)
            )
        )
        .scalars()
        .all()
    )
    assert result == []


# ── 5.11 — vision fallback retried before declaring failure ──────────────────


async def test_vision_fallback_retried_before_declaring_failure(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, contrato_dagma: Contrato, documento_dagma: DocumentoFuente
) -> None:
    # Text ladder yields nothing usable (scanned template).
    monkeypatch.setattr(document_service, "extraer_texto_documento", AsyncMock(return_value=(None, ["sin texto"])))
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"scanned-bytes"))

    # Vision chain has a usable model; multimodal support and part-building succeed.
    monkeypatch.setattr(document_service, "vision_model_chain", lambda: ["gemini/gemini-2.5-flash-lite"])
    monkeypatch.setattr("app.agent.tools.multimodal_parser.is_multimodal_supported", lambda mime: True)
    monkeypatch.setattr(
        "app.agent.tools.multimodal_parser.build_multimodal_content_parts",
        lambda content, mime, model, **kw: [{"type": "text", "text": "rasterized"}],
    )
    _patch_llm(monkeypatch, _DAGMA_2COL_JSON)

    plantilla, avisos = await svc.ingerir_plantilla_organismo(
        db, contrato_dagma.usuario_id, contrato_dagma.id, documento_dagma.id
    )
    await db.commit()

    assert plantilla is not None
    assert avisos == []
    assert plantilla.estructura_json["columnas"] == ["obligación", "avance del periodo"]


# ── Ownership / not-found guards ──────────────────────────────────────────────


async def test_ingerir_plantilla_organismo_raises_not_found_for_missing_contrato(db: AsyncSession) -> None:
    import uuid

    with pytest.raises(NotFoundError):
        await svc.ingerir_plantilla_organismo(db, uuid.uuid4(), uuid.uuid4(), uuid.uuid4())


async def test_ingerir_plantilla_organismo_raises_forbidden_for_other_user(
    db: AsyncSession, contrato_dagma: Contrato, documento_dagma: DocumentoFuente
) -> None:
    import uuid

    with pytest.raises(ForbiddenError):
        await svc.ingerir_plantilla_organismo(db, uuid.uuid4(), contrato_dagma.id, documento_dagma.id)


async def test_ingerir_plantilla_organismo_raises_validation_error_when_no_entidad(
    db: AsyncSession, test_user: dict[str, Any]
) -> None:
    user = test_user["user"]
    contrato = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-SIN-ENTIDAD-001",
        objeto="Servicios profesionales sin entidad definida",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad=None,
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=contrato.id,
        storage_key="documentos/plantilla.docx",
        nombre="plantilla.docx",
        tipo=TipoDocumentoFuente.INFORME_ACTIVIDADES,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    with pytest.raises(ValidationError):
        await svc.ingerir_plantilla_organismo(db, user.id, contrato.id, doc.id)


# ── obtener_plantilla_organismo ────────────────────────────────────────────────


async def test_obtener_plantilla_organismo_returns_none_when_not_ingested(
    db: AsyncSession, contrato_dagma: Contrato
) -> None:
    plantilla = await svc.obtener_plantilla_organismo(db, contrato_dagma.usuario_id, contrato_dagma.id)
    assert plantilla is None


async def test_obtener_plantilla_organismo_returns_persisted_structure(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch, contrato_dagma: Contrato, documento_dagma: DocumentoFuente
) -> None:
    monkeypatch.setattr(document_service, "extraer_texto_documento", AsyncMock(return_value=("texto", [])))
    monkeypatch.setattr("app.adapters.storage.get_storage", lambda *_a, **_k: _fake_storage(b"docx-bytes"))
    _patch_llm(monkeypatch, _DAGMA_2COL_JSON)

    await svc.ingerir_plantilla_organismo(db, contrato_dagma.usuario_id, contrato_dagma.id, documento_dagma.id)
    await db.commit()

    plantilla = await svc.obtener_plantilla_organismo(db, contrato_dagma.usuario_id, contrato_dagma.id)
    assert plantilla is not None
    assert plantilla.entidad_normalizada == "dagma"
