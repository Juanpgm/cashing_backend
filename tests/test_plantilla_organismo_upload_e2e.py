"""End-to-end coverage of the EXACT two-call sequence the radicación stepper's
"Subir formato de la entidad" performs (`step-5-formato.tsx::handleFile`):

    POST /documentos/upload?tipo=<informe_*|plantilla>&contrato_id=...
    POST /contratos/{contrato_id}/plantillas-organismo/  {documento_fuente_id}

Every existing plantilla-organismo test hand-builds the `DocumentoFuente` row,
so nothing covered the seam between the two calls — an upload whose resulting
`tipo`/storage_key the ingestion then has to accept and re-download. These tests
drive the real endpoints, the real storage round-trip and the real
docx skeleton/validation layer; only the LLM itself is stubbed.

Edge cases covered explicitly (not just the happy path): empty file, wrong
extension, extension/content mismatch, a `.docx` that is not a readable
document, an ingestion source with a non-plantilla `tipo`, a contrato with no
entidad, and re-uploading over an existing plantilla (the unique-constraint
replace path).
"""

from __future__ import annotations

import json
from datetime import date
from io import BytesIO
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.plantilla_organismo import PlantillaOrganismo
from app.schemas.plantilla_organismo import CamposPlantillaLLM, EstructuraPlantillaLLM
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _build_informe_docx() -> bytes:
    """A minimal stand-in for a real entity informe template: one labelled
    paragraph plus a 2-column table, i.e. both address families the skeleton
    extractor emits (`P{i}` and `T{t}.R{r}.C{c}`)."""
    from docx import Document

    doc = Document()
    doc.add_paragraph("CONTRATO No. 4161.010.26.1.155.2026")
    doc.add_paragraph("Nombre completo: JUAN PABLO GUZMAN MARTINEZ")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "OBLIGACIONES"
    table.cell(0, 1).text = "RESULTADOS"
    table.cell(1, 0).text = "A) Ejecutar los procesos"
    table.cell(1, 1).text = "Se ejecutaron los procesos del periodo."
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLM:
    """Answers both structured calls the ingestion makes, keyed on the
    `response_format` schema it is asked for. Campo addresses match the
    document `_build_informe_docx` produces, so the REAL `validar_campos`
    still has to accept them — the stub can't fake a clonable result."""

    def __init__(self, campos: list[dict[str, Any]] | None = None) -> None:
        self._campos = campos if campos is not None else _DEFAULT_CAMPOS

    async def complete(self, _messages: Any, **kwargs: Any) -> _FakeResponse:
        if kwargs.get("response_format") is CamposPlantillaLLM:
            return _FakeResponse(json.dumps({"campos": self._campos}))
        return _FakeResponse(EstructuraPlantillaLLM(notas="informe").model_dump_json())


_DEFAULT_CAMPOS: list[dict[str, Any]] = [
    {
        "direccion": "P0",
        "etiqueta": "CONTRATO No.",
        "campo": "contrato.numero_contrato",
        "valor_ejemplo": "4161.010.26.1.155.2026",
        "modo": "substring",
    },
    {
        "direccion": "P1",
        "etiqueta": "Nombre completo",
        "campo": "contratista.nombre",
        "valor_ejemplo": "JUAN PABLO GUZMAN MARTINEZ",
        "modo": "substring",
    },
    {
        "direccion": "T0.R1.C1",
        "etiqueta": "A)",
        "campo": "justificacion.1",
        "valor_ejemplo": "Se ejecutaron los procesos del periodo.",
        "modo": "justificacion",
    },
]


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.adapters.llm.get_llm", lambda *_a, **_k: _FakeLLM())


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    c = Contrato(
        usuario_id=test_user["user"].id,
        numero_contrato="CTR-E2E-PO-001",
        objeto="Servicios profesionales",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2026, 1, 1),
        fecha_fin=date(2026, 12, 31),
        entidad="DISTRITO SANTIAGO DE CALI",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _subir(
    client: AsyncClient,
    test_user: dict[str, Any],
    contrato: Contrato,
    *,
    contenido: bytes,
    nombre: str = "informe.docx",
    mime: str = DOCX_MIME,
    tipo: str = "informe_actividades",
) -> Any:
    return await client.post(
        "/api/v1/documentos/upload",
        headers=test_user["headers"],
        params={"tipo": tipo, "contrato_id": str(contrato.id)},
        files={"file": (nombre, contenido, mime)},
    )


async def _ingerir(client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, documento_id: str) -> Any:
    return await client.post(
        f"/api/v1/contratos/{contrato.id}/plantillas-organismo/",
        headers=test_user["headers"],
        json={"documento_fuente_id": documento_id},
    )


# ── Happy path: the full stepper sequence ────────────────────────────────────


async def test_upload_then_ingest_produces_a_clonable_plantilla(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato: Contrato,
    fake_llm: None,
) -> None:
    """The exact frontend sequence must end with a persisted, clonable
    plantilla whose campos survived the real deterministic validator."""
    subida = await _subir(client, test_user, contrato, contenido=_build_informe_docx())
    assert subida.status_code == 201, subida.text
    documento_id = subida.json()["id"]

    resp = await _ingerir(client, test_user, contrato, documento_id)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] is not None
    assert data["tipo_documento"] == "informe_actividades"
    assert data["formato"] == "docx"
    assert data["clonable"] is True
    assert data["fuente_documento_id"] == documento_id
    # Campos are grouped by scope prefix and survived `validar_campos`.
    assert set(data["campos"]) == {"contrato", "contratista", "justificacion"}

    persisted = (
        (await db.execute(select(PlantillaOrganismo).where(PlantillaOrganismo.usuario_id == test_user["user"].id)))
        .scalars()
        .all()
    )
    assert len(persisted) == 1
    assert persisted[0].entidad_normalizada


async def test_informe_supervision_lands_on_its_own_plantilla_row(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, fake_llm: None
) -> None:
    """The two informe types are distinct `tipo_documento`s — uploading one
    must never overwrite the other (they share the unique constraint's
    usuario/entidad half)."""
    actividades = await _subir(client, test_user, contrato, contenido=_build_informe_docx())
    await _ingerir(client, test_user, contrato, actividades.json()["id"])

    supervision = await _subir(client, test_user, contrato, contenido=_build_informe_docx(), tipo="informe_supervision")
    resp = await _ingerir(client, test_user, contrato, supervision.json()["id"])
    assert resp.status_code == 200, resp.text
    assert resp.json()["tipo_documento"] == "informe_supervision"

    listado = await client.get(f"/api/v1/contratos/{contrato.id}/plantillas-organismo/", headers=test_user["headers"])
    assert {p["tipo_documento"] for p in listado.json()} == {"informe_actividades", "informe_supervision"}


async def test_reupload_replaces_the_existing_plantilla_instead_of_duplicating(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, fake_llm: None
) -> None:
    """Replacing a plantilla ("Reemplazar" in the formato step) re-ingests over
    the same (usuario, entidad, tipo) row — a second INSERT would violate
    `uq_plantilla_organismo_key` and 500."""
    primera = await _subir(client, test_user, contrato, contenido=_build_informe_docx())
    inicial = await _ingerir(client, test_user, contrato, primera.json()["id"])
    assert inicial.status_code == 200, inicial.text

    segunda = await _subir(client, test_user, contrato, contenido=_build_informe_docx(), nombre="informe-v2.docx")
    reemplazo = await _ingerir(client, test_user, contrato, segunda.json()["id"])
    assert reemplazo.status_code == 200, reemplazo.text
    assert reemplazo.json()["id"] == inicial.json()["id"]  # same row, updated
    assert reemplazo.json()["fuente_documento_id"] == segunda.json()["id"]  # now points at the new file

    filas = (
        (await db.execute(select(PlantillaOrganismo).where(PlantillaOrganismo.tipo_documento == "informe_actividades")))
        .scalars()
        .all()
    )
    assert len(filas) == 1


# ── Edge cases: malformed / rejected uploads ─────────────────────────────────


async def test_empty_file_is_rejected_with_a_readable_message(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await _subir(client, test_user, contrato, contenido=b"")
    assert resp.status_code == 422, resp.text
    assert "vacío" in resp.json()["detail"]


async def test_wrong_extension_is_rejected_before_any_storage_write(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    resp = await _subir(
        client,
        test_user,
        contrato,
        contenido=b"MZ\x90\x00ejecutable",
        nombre="formato.exe",
        mime="application/octet-stream",
    )
    assert resp.status_code == 422, resp.text
    assert "no permitido" in resp.json()["detail"]


async def test_content_that_does_not_match_the_docx_extension_is_rejected(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato
) -> None:
    """A renamed PDF/legacy .doc announced as a .docx: the magic-byte check
    must catch it instead of letting an unparseable file reach ingestion."""
    resp = await _subir(client, test_user, contrato, contenido=b"%PDF-1.4\nno soy un docx")
    assert resp.status_code == 422, resp.text
    assert "no coincide" in resp.json()["detail"]


async def test_corrupt_docx_degrades_gracefully_instead_of_erroring(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, fake_llm: None
) -> None:
    """A file with a valid ZIP signature but no readable document body passes
    upload validation, so ingestion is what has to survive it: the contract is
    `clonable=false` + avisos (what the UI shows as "Detección incompleta"),
    never a 5xx that would strand the user with no explanation."""
    corrupto = b"PK\x03\x04" + b"\x00" * 512
    subida = await _subir(client, test_user, contrato, contenido=corrupto)
    assert subida.status_code == 201, subida.text

    resp = await _ingerir(client, test_user, contrato, subida.json()["id"])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["clonable"] is False
    assert data["avisos"]


async def test_ingesting_a_non_plantilla_document_type_is_rejected_explicitly(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, fake_llm: None
) -> None:
    """Only informe_actividades / informe_supervision / plantilla may become a
    plantilla de organismo — a RUT uploaded by mistake must say so."""
    subida = await _subir(client, test_user, contrato, contenido=_build_informe_docx(), tipo="rut")
    assert subida.status_code == 201, subida.text

    resp = await _ingerir(client, test_user, contrato, subida.json()["id"])
    assert resp.status_code == 422, resp.text
    assert "plantilla institucional" in resp.json()["detail"]


async def test_contrato_without_entidad_explains_why_ingestion_cannot_proceed(
    client: AsyncClient, db: AsyncSession, test_user: dict[str, Any], contrato: Contrato, fake_llm: None
) -> None:
    """The plantilla is keyed on the contracting entity, so a contrato with no
    `entidad` cannot own one — the user needs to be told, not 500'd."""
    contrato.entidad = ""
    await db.commit()

    subida = await _subir(client, test_user, contrato, contenido=_build_informe_docx())
    resp = await _ingerir(client, test_user, contrato, subida.json()["id"])
    assert resp.status_code == 422, resp.text
    assert "entidad contratante" in resp.json()["detail"]


async def test_campos_the_document_does_not_actually_contain_are_discarded(
    client: AsyncClient, test_user: dict[str, Any], contrato: Contrato, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The LLM proposes, `validar_campos` disposes: a hallucinated address or a
    valor_ejemplo absent from the node must be dropped with an aviso, and a
    plantilla left with zero valid campos must report clonable=false rather
    than advertising a clone that would silently produce a blank document."""
    monkeypatch.setattr(
        "app.adapters.llm.get_llm",
        lambda *_a, **_k: _FakeLLM(
            campos=[
                {
                    "direccion": "P99",  # out of range
                    "etiqueta": "Inexistente",
                    "campo": "contrato.objeto",
                    "valor_ejemplo": "x",
                    "modo": "cell",
                },
                {
                    "direccion": "P0",  # exists, but the value does not appear in it
                    "etiqueta": "No coincide",
                    "campo": "contrato.entidad",
                    "valor_ejemplo": "VALOR QUE NO ESTA EN EL DOCUMENTO",
                    "modo": "substring",
                },
            ]
        ),
    )
    subida = await _subir(client, test_user, contrato, contenido=_build_informe_docx())
    resp = await _ingerir(client, test_user, contrato, subida.json()["id"])

    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["clonable"] is False
    assert data["campos"] == {}
    assert len(data["avisos"]) == 2
