"""B4: replacing the contract document must clear its previous obligations.

`_persist_obligaciones` only APPENDS (dedup by normalized text), so obligations
extracted from a REPLACED contract document survived every subsequent replace
and kept accumulating alongside the new ones. Decision (from the incident):
always clear on replace, even when the new extraction yields 0 items — log a
warning in that case rather than silently leaving 0 obligations unexplained.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import structlog
from app.models.contrato import Contrato
from app.models.obligacion import Obligacion
from app.services.document_service import upload_document
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

_PATCH_S3 = "app.services.document_service._get_storage"

# 200+ chars so is_text_sufficient() passes without OCR/vision mocks.
_TEXTO_CON_OBLIGACIONES = (
    "CLÁUSULA SEGUNDA. OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
    "1. Elaborar los estudios previos de los procesos de contratacion.\n"
    "2. Revisar los actos administrativos que expida la entidad.\n"
    "3. Las demás actividades que le asigne la supervisión relacionadas con el objeto del contrato.\n"
)
# No obligations section at all — a clean, unrelated document.
_TEXTO_SIN_OBLIGACIONES = "Acuerdo de confidencialidad sin cláusulas de obligaciones específicas. " * 5


def _mock_storage() -> AsyncMock:
    storage = AsyncMock()
    storage.upload = AsyncMock()
    storage.delete = AsyncMock()
    return storage


async def _crear_contrato(db: AsyncSession, user_id: uuid.UUID, numero: str = "CD-REPLACE-001") -> Contrato:
    contrato = Contrato(
        usuario_id=user_id,
        numero_contrato=numero,
        objeto="Objeto de prueba",
        valor_total=12_000_000.0,
        valor_mensual=1_000_000.0,
        fecha_inicio=date(2025, 1, 1),
        fecha_fin=date(2025, 12, 31),
    )
    db.add(contrato)
    await db.commit()
    await db.refresh(contrato)
    return contrato


async def _obligaciones(db: AsyncSession, contrato_id: uuid.UUID) -> list[Obligacion]:
    res = await db.execute(select(Obligacion).where(Obligacion.contrato_id == contrato_id))
    return list(res.scalars().all())


async def _subir_contrato(db: AsyncSession, user_id: uuid.UUID, contrato_id: uuid.UUID, filename: str, texto: str):
    with patch(_PATCH_S3) as mock_storage_cls:
        mock_storage_cls.return_value = _mock_storage()
        return await upload_document(
            db=db,
            user_id=user_id,
            filename=filename,
            content=texto.encode("utf-8"),
            content_type="text/plain",
            contrato_id=contrato_id,
            requisito_codigo="CONTRATO",
        )


class _NoOpLLM:
    """Fails fast — no network calls. `extract_obligaciones_verbatim` already
    returns [] for `_TEXTO_SIN_OBLIGACIONES` (no obligations section), so this
    only needs to make the Step-2 LLM fallback resolve to 0 items quickly."""

    def __init__(self, model: str | None = None) -> None: ...

    async def complete(self, messages: list[object], **kwargs: object) -> None:
        raise RuntimeError("no LLM configured for this test")


class TestReplaceWithZeroExtractedClearsAndWarns:
    async def test_replace_with_no_obligations_section_leaves_zero_and_logs_warning(
        self, db: AsyncSession, test_user: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.adapters.llm as llm_module

        monkeypatch.setattr(llm_module, "get_llm", lambda model=None: _NoOpLLM(model))

        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)

        await _subir_contrato(db, user.id, contrato.id, "contrato-v1.txt", _TEXTO_CON_OBLIGACIONES)
        antes = await _obligaciones(db, contrato.id)
        assert antes, "precondition: v1 must have produced obligations"

        with structlog.testing.capture_logs() as events:
            await _subir_contrato(db, user.id, contrato.id, "contrato-v2-sin-obligaciones.txt", _TEXTO_SIN_OBLIGACIONES)

        despues = await _obligaciones(db, contrato.id)
        assert despues == [], f"replacing with a doc with no obligations must leave zero, got: {despues}"

        warned = [e for e in events if e.get("event") == "contrato_reemplazado_sin_obligaciones"]
        assert warned, f"expected a contrato_reemplazado_sin_obligaciones warning, got events: {events}"


class TestReplaceThenReplace:
    async def test_second_replace_does_not_resurrect_the_first_documents_obligations(
        self, db: AsyncSession, test_user: dict[str, Any]
    ) -> None:
        """v1 -> v2 -> v3: only v3's obligations must remain; neither v1's nor
        v2's should linger or reappear."""
        user = test_user["user"]
        contrato = await _crear_contrato(db, user.id)

        texto_v1 = _TEXTO_CON_OBLIGACIONES.replace("estudios previos", "estudios previos V1")
        texto_v2 = _TEXTO_CON_OBLIGACIONES.replace("estudios previos", "estudios previos V2")
        texto_v3 = _TEXTO_CON_OBLIGACIONES.replace("estudios previos", "estudios previos V3")

        await _subir_contrato(db, user.id, contrato.id, "contrato-v1.txt", texto_v1)
        await _subir_contrato(db, user.id, contrato.id, "contrato-v2.txt", texto_v2)
        await _subir_contrato(db, user.id, contrato.id, "contrato-v3.txt", texto_v3)

        descripciones = {o.descripcion for o in await _obligaciones(db, contrato.id)}
        assert any("V3" in d for d in descripciones), "v3's obligations must be present"
        assert not any("V1" in d for d in descripciones), "v1's obligations must not linger"
        assert not any("V2" in d for d in descripciones), "v2's obligations must not linger"
