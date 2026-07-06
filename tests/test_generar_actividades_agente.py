"""Tests for `cuenta_cobro_service.generar_actividades_agente`.

Reproduce y previene la regresión: cuando un contrato tiene texto del
contrato cargado pero NO obligaciones registradas, el agente debe poder
generar actividades sueltas (`obligacion_id=None`) y devolver al menos las
mismas que produjo el LLM.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion, TipoObligacion
from app.schemas.agent import LLMResponse
from app.services import cuenta_cobro_service

pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato_con_doc(
    db: AsyncSession, test_user: dict[str, Any]
) -> Contrato:
    """Contrato SIN obligaciones pero CON documento de contrato (texto)."""
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-AGT-001",
        objeto="Servicios profesionales en seguridad y justicia",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Alcaldía",
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
        texto_extraido=(
            "OBJETO: Prestación de servicios profesionales para apoyar la "
            "Secretaría de Seguridad. ACTIVIDADES: 1) elaborar informes "
            "mensuales 2) participar en reuniones 3) asistir a eventos 4) "
            "actualizar conocimientos técnicos."
        ),
    )
    db.add(doc)
    await db.commit()
    return c


@pytest.fixture
async def contrato_vacio(
    db: AsyncSession, test_user: dict[str, Any]
) -> Contrato:
    """Contrato sin obligaciones y sin documentos."""
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-AGT-002",
        objeto="Consultoría",
        valor_total=10_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def contrato_con_obligaciones(
    db: AsyncSession, test_user: dict[str, Any]
) -> tuple[Contrato, list[Obligacion]]:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-AGT-003",
        objeto="Servicios",
        valor_total=12_000_000,
        valor_mensual=1_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)

    obs = [
        Obligacion(
            contrato_id=c.id,
            descripcion=f"Obligación contractual número {i + 1} con texto suficiente",
            tipo=TipoObligacion.GENERAL,
            orden=i,
        )
        for i in range(3)
    ]
    db.add_all(obs)
    await db.commit()
    for o in obs:
        await db.refresh(o)
    c.obligaciones = list(obs)
    return c, obs


async def _make_cuenta(db: AsyncSession, contrato: Contrato) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=5,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=contrato.valor_mensual,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    return cc


class _FakeLLM:
    def __init__(self, response_content: str) -> None:
        self._content = response_content

    async def complete(self, messages, temperature=0.3, max_tokens=4096) -> LLMResponse:  # noqa: ARG002
        return LLMResponse(
            content=self._content,
            model="fake/test-model",
            prompt_tokens=10,
            completion_tokens=20,
            total_tokens=30,
        )


def _patch_llm(monkeypatch: pytest.MonkeyPatch, content: str) -> None:
    """Monkeypatch the late-imported `get_llm` so the service uses _FakeLLM."""
    fake = _FakeLLM(content)
    import app.adapters.llm as llm_pkg

    monkeypatch.setattr(llm_pkg, "get_llm", lambda model=None: fake, raising=True)


# ── Tests ──────────────────────────────────────────────────────────────────


async def test_genera_actividades_sin_obligaciones_con_texto_contrato(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_con_doc: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regresión: contrato con texto pero sin obligaciones → actividades sueltas."""
    cuenta = await _make_cuenta(db, contrato_con_doc)
    user = test_user["user"]

    llm_response = (
        "ACTIVIDAD|Elaboré y presenté el informe mensual de avance al supervisor"
        "|Cumplimiento del objeto contractual de apoyo a la Secretaría|1\n"
        "ACTIVIDAD|Participé en reuniones de coordinación con el equipo"
        "|Cumplimiento del objeto contractual|2\n"
        "ACTIVIDAD|Desarrollé y actualicé mis conocimientos técnicos en el área"
        "|Cumplimiento del objeto contractual|3\n"
        "ACTIVIDAD|Asistí a eventos relacionados con seguridad y justicia"
        "|Cumplimiento del objeto contractual|4\n"
    )
    _patch_llm(monkeypatch, llm_response)

    resp = await cuenta_cobro_service.generar_actividades_agente(
        db, user.id, cuenta.id
    )

    assert resp.creadas == 4, f"esperaba 4 actividades, obtuve {resp.creadas}"
    assert len(resp.actividades) == 4
    # Sin obligaciones registradas → todas deben quedar con obligacion_id=None
    assert all(a.obligacion_id is None for a in resp.actividades)
    # Las descripciones deben venir del LLM, no truncadas a vacío
    assert all(len(a.descripcion) >= 10 for a in resp.actividades)


async def test_genera_actividades_vincula_a_obligaciones_por_numero(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_con_obligaciones: tuple[Contrato, list[Obligacion]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contrato, obs = contrato_con_obligaciones
    cuenta = await _make_cuenta(db, contrato)
    user = test_user["user"]

    llm_response = (
        "ACTIVIDAD|Elaboré informe mensual completo y detallado|Cumplimiento ob. 1|1\n"
        "ACTIVIDAD|Participé activamente en reuniones de coordinación|Cumplimiento ob. 2|2\n"
        "ACTIVIDAD|Desarrollé y entregué los entregables solicitados|Cumplimiento ob. 3|3\n"
    )
    _patch_llm(monkeypatch, llm_response)

    resp = await cuenta_cobro_service.generar_actividades_agente(
        db, user.id, cuenta.id
    )

    assert resp.creadas == 3
    obligacion_ids = [a.obligacion_id for a in resp.actividades]
    assert obligacion_ids == [obs[0].id, obs[1].id, obs[2].id]


async def test_falla_si_no_hay_obligaciones_ni_documento(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_vacio: Contrato,
) -> None:
    cuenta = await _make_cuenta(db, contrato_vacio)
    user = test_user["user"]

    with pytest.raises(ValidationError, match="obligaciones registradas"):
        await cuenta_cobro_service.generar_actividades_agente(
            db, user.id, cuenta.id
        )


async def test_falla_si_llm_devuelve_formato_invalido(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_con_doc: Contrato,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cuenta = await _make_cuenta(db, contrato_con_doc)
    user = test_user["user"]

    _patch_llm(
        monkeypatch,
        "Texto libre que no respeta el formato ACTIVIDAD|...|...|N esperado.",
    )

    with pytest.raises(ValidationError, match="formato esperado"):
        await cuenta_cobro_service.generar_actividades_agente(
            db, user.id, cuenta.id
        )


async def test_falla_si_estado_no_permite_edicion(
    db: AsyncSession,
    test_user: dict[str, Any],
    contrato_con_doc: Contrato,
) -> None:
    cuenta = await _make_cuenta(db, contrato_con_doc)
    cuenta.estado = EstadoCuentaCobro.ENVIADA
    await db.commit()
    user = test_user["user"]

    with pytest.raises(ValidationError, match="estado"):
        await cuenta_cobro_service.generar_actividades_agente(
            db, user.id, cuenta.id
        )
