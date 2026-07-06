"""Vision fallback en la re-extracción de obligaciones (contratos escaneados).

Cuando el documento del contrato no tiene texto seleccionable (escaneado), el
endpoint de re-extracción debe bajar el archivo y leerlo con el modelo de visión,
en vez de devolver vacío. Cubre el flujo que NO es auto-creación.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion
from app.schemas.agent import ContratoExtractionResult, ObligacionItemLLM
from app.services import document_service
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class _FakeStorage:
    """Stand-in for S3StorageAdapter that returns canned bytes (no network)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    async def download(self, key: str) -> bytes:
        return b"%PDF-1.4 scanned-contract-bytes"


async def _make_scanned_contract(db: AsyncSession, user_id: Any) -> Contrato:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato="CTR-SCAN-001",
        objeto="Restauración ambiental",
        valor_total=10_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="DAGMA",
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)

    # Scanned PDF: stored with empty text (pdfplumber found no text layer).
    doc = DocumentoFuente(
        usuario_id=user_id,
        contrato_id=contrato.id,
        storage_key=f"usuarios/{user_id}/contrato-escaneado.pdf",
        nombre="contrato-escaneado.pdf",
        tipo=TipoDocumentoFuente.CONTRATO,
        texto_extraido="",
    )
    db.add(doc)
    await db.commit()
    return contrato


@pytest.mark.asyncio
async def test_reextraccion_usa_vision_cuando_no_hay_texto(
    db: AsyncSession,
    test_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Documento escaneado → cae a visión y persiste las obligaciones que devuelve."""
    user = test_user["user"]
    contrato = await _make_scanned_contract(db, user.id)

    vision_result = ContratoExtractionResult(
        obligaciones=[
            ObligacionItemLLM(
                descripcion="Realizar el diagnóstico ambiental del predio priorizado",
                tipo="especifica",
                etiqueta="1",
            ),
            ObligacionItemLLM(
                descripcion="Las demás que asigne la supervisión relacionadas con el objeto del contrato",
                tipo="especifica",
                etiqueta="2",
            ),
        ],
        transcripcion="TEXTO OCR COMPLETO DEL CONTRATO ESCANEADO PRODUCIDO POR EL MODELO DE VISIÓN.",
    )

    async def _fake_multimodal(content: bytes, mime: str) -> ContratoExtractionResult:
        return vision_result

    monkeypatch.setattr(document_service, "_extraer_contrato_multimodal", _fake_multimodal)
    monkeypatch.setattr(document_service, "_get_storage", _FakeStorage)
    monkeypatch.setattr(document_service.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", True)

    obligaciones, _avisos = await document_service.extraer_obligaciones_documento(
        contrato_id=contrato.id,
        user_id=user.id,
        db=db,
    )

    assert len(obligaciones) == 2
    assert obligaciones[0].descripcion == "Realizar el diagnóstico ambiental del predio priorizado"

    rows = (
        (await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato.id).order_by(Obligacion.orden)))
        .scalars()
        .all()
    )
    assert len(rows) == 2

    # La transcripción del modelo de visión se cachea para futuras corridas.
    doc = (await db.execute(select(DocumentoFuente).where(DocumentoFuente.contrato_id == contrato.id))).scalar_one()
    assert doc.texto_extraido.startswith("TEXTO OCR COMPLETO")


@pytest.mark.asyncio
async def test_reextraccion_avisa_cuando_vision_deshabilitada(
    db: AsyncSession,
    test_user: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sin texto y con visión deshabilitada → 0 obligaciones + aviso accionable."""
    user = test_user["user"]
    contrato = await _make_scanned_contract(db, user.id)
    # No usable fallback: both vision and local OCR disabled (OCR may be installed
    # in this environment, so disable it explicitly to test the "no path" aviso).
    monkeypatch.setattr(document_service.settings, "EXTRACTION_MULTIMODAL_FALLBACK_ENABLED", False)
    monkeypatch.setattr(document_service.settings, "EXTRACTION_OCR_ENABLED", False)

    obligaciones, avisos = await document_service.extraer_obligaciones_documento(
        contrato_id=contrato.id,
        user_id=user.id,
        db=db,
    )

    assert obligaciones == []
    assert any("visión" in a.lower() for a in avisos)
