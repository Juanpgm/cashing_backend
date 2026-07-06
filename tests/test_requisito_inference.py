"""Tests for app.services.requisito_inference_service."""

from __future__ import annotations

import pytest
from app.schemas.agent import LLMResponse
from app.services import requisito_inference_service as svc
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


class _FakeLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def complete(
        self, messages, temperature=0.0, max_tokens=4096, response_format=None, **kwargs
    ) -> LLMResponse:
        return LLMResponse(
            content=self._content,
            model="fake/test-model",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: _FakeLLM(content), raising=True)


async def test_infiere_y_normaliza_codigo_y_keywords(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(
        monkeypatch,
        '{"requisitos": [{"codigo": "póliza cumplimiento", "etiqueta": "Póliza de cumplimiento",'
        ' "obligatorio": true, "keywords_deteccion": ["Póliza", "CUMPLIMIENTO", "poliza"]}]}',
    )

    preview = await svc.inferir_requisitos(db, "El contratista debe aportar póliza de cumplimiento.")

    assert len(preview.requisitos) == 1
    item = preview.requisitos[0]
    assert item.codigo == "POLIZA_CUMPLIMIENTO"
    # lowercased + de-duplicated, accents preserved as provided then lowered
    assert item.keywords_deteccion == ["poliza", "cumplimiento"]
    assert item.origen == "inferido"
    assert item.id is None


async def test_mapea_a_estandar_por_codigo(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, '{"requisitos": [{"codigo": "RUT", "etiqueta": "RUT actualizado"}]}')

    preview = await svc.inferir_requisitos(db, "Aporte el RUT.")

    assert preview.requisitos[0].mapea_a_estandar == "RUT"


async def test_mapea_a_estandar_por_hint(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(
        monkeypatch,
        '{"requisitos": [{"codigo": "DOC_IDENTIDAD", "etiqueta": "Documento de identidad",'
        ' "mapea_a_estandar": "CEDULA"}]}',
    )

    preview = await svc.inferir_requisitos(db, "Adjunte documento de identidad.")

    assert preview.requisitos[0].mapea_a_estandar == "CEDULA"


async def test_dedup_inferidos_por_codigo(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(
        monkeypatch,
        '{"requisitos": [{"codigo": "RUP", "etiqueta": "RUP"},'
        ' {"codigo": "rup", "etiqueta": "Registro único de proponentes"}]}',
    )

    preview = await svc.inferir_requisitos(db, "Aporte el RUP.")

    assert len(preview.requisitos) == 1
    assert preview.requisitos[0].codigo == "RUP"


async def test_json_invalido_devuelve_vacio_con_aviso(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_llm(monkeypatch, "esto no es json")

    preview = await svc.inferir_requisitos(db, "texto cualquiera")

    assert preview.requisitos == []
    assert preview.avisos


async def test_texto_vacio_no_llama_llm(db: AsyncSession) -> None:
    preview = await svc.inferir_requisitos(db, "   ")
    assert preview.requisitos == []
    assert preview.avisos


async def test_desde_archivo_usa_extraccion_y_infiere(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.document_service as doc_svc

    async def _fake_extraer(content: bytes, filename: str):
        return "El contratista debe aportar póliza de cumplimiento.", []

    monkeypatch.setattr(doc_svc, "extraer_texto_documento", _fake_extraer, raising=True)
    _patch_llm(
        monkeypatch,
        '{"requisitos": [{"codigo": "POLIZA_CUMPLIMIENTO", "etiqueta": "Póliza de cumplimiento"}]}',
    )

    preview = await svc.inferir_requisitos_desde_archivo(
        db, filename="pliego.pdf", content=b"%PDF-fake", content_type="application/pdf"
    )

    assert len(preview.requisitos) == 1
    assert preview.requisitos[0].codigo == "POLIZA_CUMPLIMIENTO"


async def test_desde_archivo_sin_texto_devuelve_avisos(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.document_service as doc_svc

    async def _fake_extraer(content: bytes, filename: str):
        return None, ["No se pudo extraer texto legible del documento."]

    monkeypatch.setattr(doc_svc, "extraer_texto_documento", _fake_extraer, raising=True)

    preview = await svc.inferir_requisitos_desde_archivo(
        db, filename="scan.png", content=b"fake", content_type="image/png"
    )

    assert preview.requisitos == []
    assert preview.avisos
