"""Tests for verbatim obligation extraction.

Garantiza que el texto de cada obligación se preserva EXACTAMENTE como
aparece en el contrato — sin parafraseo, resumen ni reordenamiento.

Pipeline cubierto:
1. `extract_obligaciones_verbatim` (regex determinista, vía preferida).
2. `parse_obligaciones_llm` (parser de salida del LLM, vía fallback).
3. `extraer_obligaciones_documento` (servicio extremo a extremo).
4. Endpoint REST `POST /api/v1/contratos/{id}/obligaciones/extraer`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.tools.contract_parser import (
    extract_obligaciones_verbatim,
    parse_obligaciones_llm,
)
from app.models.contrato import Contrato
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion
from app.schemas.agent import LLMResponse
from app.services import document_service

# Solo los tests async necesitan el marker; los unitarios síncronos no.
asyncio_test = pytest.mark.asyncio


# ── Sample contract text (5 specific obligations, numbered) ────────────────

OBLIGACIONES_VERBATIM = [
    "Diseñar e implementar los módulos del sistema de información SISBEN IV "
    "según los requerimientos técnicos definidos por la Secretaría",
    "Realizar las pruebas unitarias y de integración de cada componente "
    "antes de su despliegue en el ambiente de producción",
    "Capacitar a los funcionarios de la Alcaldía de Cali en el uso del "
    "nuevo sistema mediante talleres presenciales mensuales",
    "Documentar los procedimientos técnicos y manuales de usuario en "
    "español, con ejemplos y capturas de pantalla",
    "Brindar soporte técnico de segundo nivel durante los noventa (90) "
    "días posteriores a la puesta en producción",
]

TEXTO_CONTRATO = f"""
CONTRATO DE PRESTACIÓN DE SERVICIOS PROFESIONALES Nº 2024-0123

ENTRE LA ALCALDÍA DE SANTIAGO DE CALI Y EL CONTRATISTA, SE CELEBRA EL
PRESENTE CONTRATO, PREVIAS LAS SIGUIENTES CONSIDERACIONES:

CLÁUSULA PRIMERA — OBJETO: El contratista se obliga a prestar sus
servicios profesionales para el desarrollo del sistema SISBEN IV.

CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:

1. {OBLIGACIONES_VERBATIM[0]}.
2. {OBLIGACIONES_VERBATIM[1]}.
3. {OBLIGACIONES_VERBATIM[2]}.
4. {OBLIGACIONES_VERBATIM[3]}.
5. {OBLIGACIONES_VERBATIM[4]}.

CLÁUSULA TERCERA — OBLIGACIONES GENERALES: El contratista deberá pagar
los aportes a seguridad social, guardar confidencialidad de la
información y asistir a las reuniones convocadas por el supervisor.

CLÁUSULA CUARTA — VALOR DEL CONTRATO: Treinta y seis millones de pesos.
""".strip()


# ── Unit tests: deterministic verbatim extractor ───────────────────────────


def test_verbatim_extractor_recovers_all_five_obligations() -> None:
    """El extractor regex debe encontrar las 5 obligaciones específicas."""
    result = extract_obligaciones_verbatim(TEXTO_CONTRATO)
    assert len(result) == 5
    for ob in result:
        assert ob.tipo == "especifica"


def test_verbatim_extractor_preserves_exact_text() -> None:
    """Cada descripción extraída debe ser substring textual del contrato."""
    result = extract_obligaciones_verbatim(TEXTO_CONTRATO)
    for ob in result:
        assert ob.descripcion in TEXTO_CONTRATO, (
            f"La descripción extraída no aparece literalmente en el contrato:\n{ob.descripcion!r}"
        )


def test_verbatim_extractor_matches_expected_items_in_order() -> None:
    """El orden y contenido coinciden con los items numerados del contrato."""
    result = extract_obligaciones_verbatim(TEXTO_CONTRATO)
    for i, ob in enumerate(result):
        assert ob.descripcion == OBLIGACIONES_VERBATIM[i]
        assert ob.orden == i


def test_verbatim_extractor_captures_numeric_markers() -> None:
    """Los marcadores numéricos originales se capturan en 'etiqueta'."""
    result = extract_obligaciones_verbatim(TEXTO_CONTRATO)
    expected_markers = ["1", "2", "3", "4", "5"]
    for ob, expected in zip(result, expected_markers, strict=True):
        assert ob.etiqueta == expected, (
            f"Marcador incorrecto para '{ob.descripcion[:40]}…': "
            f"esperado {expected!r}, obtenido {ob.etiqueta!r}"
        )


def test_verbatim_extractor_captures_alpha_markers() -> None:
    """Los marcadores alfabéticos originales (a, b, c) se capturan en 'etiqueta'."""
    texto = (
        "OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
        "a) Realizar el diagnóstico territorial del municipio.\n"
        "b) Elaborar los estudios previos para los proyectos de inversión.\n"
        "c) Apoyar la formulación del Plan de Desarrollo Municipal 2024-2027.\n"
        "\nVALOR DEL CONTRATO: cinco millones."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 3
    assert [ob.etiqueta for ob in result] == ["a", "b", "c"]


def test_verbatim_extractor_includes_catch_all_item() -> None:
    """El ítem de cierre 'Las demás actividades…' se incluye en el listado."""
    texto = (
        "OBLIGACIONES ESPECÍFICAS:\n"
        "1. Diseñar el sistema de información según los requerimientos técnicos.\n"
        "2. Capacitar a los funcionarios en el uso del sistema.\n"
        "3. Las demás actividades que le asigne la Secretaría y que se relacionen "
        "con el objeto del contrato y garanticen la adecuada prestación del servicio.\n"
        "\nFORMA DE PAGO: mensual."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 3
    assert "Las demás actividades" in result[-1].descripcion
    assert result[-1].etiqueta == "3"


def test_verbatim_extractor_excludes_obligaciones_generales() -> None:
    """No debe extraer items de la sección OBLIGACIONES GENERALES."""
    result = extract_obligaciones_verbatim(TEXTO_CONTRATO)
    joined = " ".join(ob.descripcion for ob in result).lower()
    assert "seguridad social" not in joined
    assert "confidencialidad" not in joined


def test_verbatim_extractor_supports_letter_enumeration() -> None:
    """Items con enumeración alfabética (a), b), c)) también funcionan."""
    texto = (
        "OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
        "a) Realizar el diagnóstico territorial del municipio.\n"
        "b) Elaborar los estudios previos para los proyectos de inversión.\n"
        "c) Apoyar la formulación del Plan de Desarrollo Municipal 2024-2027.\n"
        "\nVALOR DEL CONTRATO: cinco millones."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 3
    assert result[0].descripcion == "Realizar el diagnóstico territorial del municipio"
    for ob in result:
        assert ob.descripcion in texto


def test_verbatim_extractor_handles_wrapped_lines() -> None:
    """Items que se parten en varias líneas se reconstruyen correctamente."""
    texto = (
        "OBLIGACIONES ESPECÍFICAS:\n"
        "1. Diseñar la malla curricular del programa técnico\n"
        "   en Análisis y Desarrollo de Software de acuerdo\n"
        "   con los lineamientos institucionales.\n"
        "2. Ejecutar las sesiones de formación según el calendario.\n"
        "FORMA DE PAGO: mensual."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 2
    assert "lineamientos institucionales" in result[0].descripcion
    assert "Análisis y Desarrollo de Software" in result[0].descripcion


def test_verbatim_extractor_returns_empty_when_no_section() -> None:
    """Sin sección reconocible, devuelve [] para que el LLM tome el relevo."""
    texto = "Texto del contrato sin secciones marcadas claramente, solo prosa libre."
    assert extract_obligaciones_verbatim(texto) == []


def test_verbatim_extractor_returns_empty_for_empty_input() -> None:
    assert extract_obligaciones_verbatim("") == []


# ── Header variant tests ───────────────────────────────────────────────────


def test_verbatim_extractor_handles_actividades_especificas_header() -> None:
    """'ACTIVIDADES ESPECÍFICAS' es tier-1 equivalente a 'OBLIGACIONES ESPECÍFICAS'."""
    texto = (
        "ACTIVIDADES ESPECÍFICAS DEL CONTRATISTA:\n"
        "1. Elaborar el diagnóstico de necesidades de capacitación del municipio.\n"
        "2. Diseñar el plan de formación anual con base en el diagnóstico.\n"
        "3. Ejecutar las sesiones de capacitación según el cronograma aprobado.\n"
        "\nVALOR DEL CONTRATO: diez millones."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 3
    assert result[0].descripcion == "Elaborar el diagnóstico de necesidades de capacitación del municipio"
    for ob in result:
        assert ob.tipo == "especifica"
        assert ob.descripcion in texto


def test_verbatim_extractor_handles_actividades_especificas_sin_acento() -> None:
    """'ACTIVIDADES ESPECIFICAS' (sin tilde) también dispara tier-1."""
    texto = (
        "ACTIVIDADES ESPECIFICAS:\n"
        "a) Revisar y actualizar los procedimientos del área de contratación.\n"
        "b) Elaborar informes de seguimiento mensual para el supervisor.\n"
        "\nFORMA DE PAGO: mensual."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 2
    assert "Revisar y actualizar" in result[0].descripcion
    assert "Elaborar informes" in result[1].descripcion


def test_verbatim_extractor_handles_actividades_del_contrato_tier2() -> None:
    """'ACTIVIDADES DEL CONTRATO' (tier-2) se usa como fallback cuando no hay tier-1."""
    texto = (
        "ACTIVIDADES DEL CONTRATO:\n"
        "1. Prestar asesoría jurídica en los procesos de selección abreviada.\n"
        "2. Revisar los pliegos de condiciones antes de su publicación en SECOP.\n"
        "\nPLAZO DE EJECUCIÓN: seis meses."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 2
    assert "asesoría jurídica" in result[0].descripcion


def test_verbatim_extractor_handles_funciones_del_contratista_tier2() -> None:
    """'FUNCIONES DEL CONTRATISTA' (tier-2) extrae ítems correctamente."""
    texto = (
        "FUNCIONES DEL CONTRATISTA:\n"
        "- Coordinar con el equipo de sistemas la integración de APIs.\n"
        "- Documentar los requerimientos funcionales del sistema.\n"
        "- Participar en las reuniones semanales de seguimiento del proyecto.\n"
        "\nSUPERVISIÓN: Jefe de la Oficina de Sistemas."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 3
    assert "integración de APIs" in result[0].descripcion


# ── Unit test: LLM-output parser preserves text ────────────────────────────


def test_parse_obligaciones_llm_preserves_exact_strings() -> None:
    """El parser no altera el texto que el LLM devuelve verbatim."""
    canned = "\n".join(
        f"OBLIGACION|especifica|{txt}" for txt in OBLIGACIONES_VERBATIM
    )
    parsed = parse_obligaciones_llm(canned)
    assert len(parsed) == 5
    for parsed_ob, expected in zip(parsed, OBLIGACIONES_VERBATIM, strict=True):
        assert parsed_ob.descripcion == expected
        assert parsed_ob.descripcion in TEXTO_CONTRATO


# ── Integration test: service persists verbatim text ───────────────────────


class _FakeLLM:
    """LLM fake que NO se debería invocar cuando el extractor regex funciona."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, messages, temperature=0.3, max_tokens=4096) -> LLMResponse:  # noqa: ARG002
        self.calls += 1
        return LLMResponse(
            content="",
            model="fake/should-not-be-called",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )


@pytest.fixture
@asyncio_test
async def contrato_con_texto(
    db: AsyncSession, test_user: dict[str, Any]
) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-VRB-001",
        objeto="Desarrollo SISBEN IV",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Alcaldía de Cali",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)

    doc = DocumentoFuente(
        usuario_id=user.id,
        contrato_id=c.id,
        storage_key=f"users/{user.id}/contrato.pdf",
        nombre="contrato.pdf",
        tipo=TipoDocumentoFuente.CONTRATO,
        texto_extraido=TEXTO_CONTRATO,
    )
    db.add(doc)
    await db.commit()
    return c


@asyncio_test
async def test_extraer_obligaciones_documento_persiste_texto_verbatim(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_con_texto: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: el servicio guarda obligaciones cuyo texto está literalmente en el contrato."""
    fake = _FakeLLM()
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: fake, raising=True)

    extraidas, avisos = await document_service.extraer_obligaciones_documento(
        contrato_id=contrato_con_texto.id,
        user_id=test_user["user"].id,
        db=db,
    )
    await db.commit()

    assert avisos == []
    assert len(extraidas) == 5
    assert fake.calls == 0, "El LLM no debería invocarse cuando el regex funciona"

    rows = (
        await db.execute(
            select(Obligacion)
            .where(Obligacion.contrato_id == contrato_con_texto.id)
            .order_by(Obligacion.orden)
        )
    ).scalars().all()
    assert len(rows) == 5
    for ob in rows:
        assert ob.descripcion in TEXTO_CONTRATO, (
            f"Obligación persistida no es verbatim:\n{ob.descripcion!r}"
        )


# ── End-to-end test: REST endpoint returns verbatim text ───────────────────


@asyncio_test
async def test_endpoint_extraer_obligaciones_devuelve_texto_verbatim(
    client: AsyncClient,
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_con_texto: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El endpoint REST expone exactamente el texto del contrato a la UI."""
    fake = _FakeLLM()
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: fake, raising=True)

    resp = await client.post(
        f"/api/v1/contratos/{contrato_con_texto.id}/obligaciones/extraer",
        headers=test_user["headers"],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["total"] == 5
    assert len(body["obligaciones"]) == 5
    for item in body["obligaciones"]:
        assert item["tipo"] == "especifica"
        assert item["descripcion"] in TEXTO_CONTRATO


# ── Dedup preserves exact text ─────────────────────────────────────────────


@asyncio_test
async def test_dedup_no_altera_texto_obligacion(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_con_texto: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Llamar dos veces no duplica filas y conserva el texto verbatim."""
    fake = _FakeLLM()
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: fake, raising=True)

    await document_service.extraer_obligaciones_documento(
        contrato_id=contrato_con_texto.id,
        user_id=test_user["user"].id,
        db=db,
    )
    await db.commit()
    await document_service.extraer_obligaciones_documento(
        contrato_id=contrato_con_texto.id,
        user_id=test_user["user"].id,
        db=db,
    )
    await db.commit()

    rows = (
        await db.execute(
            select(Obligacion).where(Obligacion.contrato_id == contrato_con_texto.id)
        )
    ).scalars().all()
    assert len(rows) == 5  # no duplicados
    for ob in rows:
        assert ob.descripcion in TEXTO_CONTRATO
