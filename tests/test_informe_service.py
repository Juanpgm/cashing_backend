"""Tests for informe_service (DOCX + ZIP generators)."""

from __future__ import annotations

import io
import uuid
import zipfile
from datetime import date
from typing import Any

import pytest
from docx import Document
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, ValidationError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.obligacion import Obligacion, TipoObligacion
from app.services import informe_service

pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-INF-001",
        objeto="Servicios profesionales de desarrollo de software",
        valor_total=36_000_000,
        valor_mensual=3_000_000,
        fecha_inicio=date(2024, 1, 1),
        fecha_fin=date(2024, 12, 31),
        entidad="Alcaldía",
        dependencia="TI",
        supervisor_nombre="Carlos Supervisor",
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


@pytest.fixture
async def obligaciones(db: AsyncSession, contrato: Contrato) -> list[Obligacion]:
    obs = [
        Obligacion(
            contrato_id=contrato.id,
            descripcion=f"Obligación contractual #{i + 1} con un texto razonablemente largo",
            tipo=TipoObligacion.GENERAL,
            orden=i,
        )
        for i in range(3)
    ]
    db.add_all(obs)
    await db.commit()
    for o in obs:
        await db.refresh(o)
    # The contrato was loaded with its `obligaciones` collection empty (lazy
    # selectin). Manually attach the new obligations so subsequent loads in
    # the same async session see them.
    contrato.obligaciones = list(obs)
    return obs


@pytest.fixture
async def cuenta(
    db: AsyncSession, contrato: Contrato, obligaciones: list[Obligacion]
) -> CuentaCobro:
    cc = CuentaCobro(
        contrato_id=contrato.id,
        mes=5,
        anio=2024,
        estado=EstadoCuentaCobro.BORRADOR,
        valor=3_000_000,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)

    for i, ob in enumerate(obligaciones):
        act = Actividad(
            cuenta_cobro_id=cc.id,
            obligacion_id=ob.id,
            descripcion=f"Actividad realizada {i + 1}",
            justificacion=f"Justificación detallada {i + 1}",
            fecha_realizacion=date(2024, 5, 10 + i),
        )
        db.add(act)
    await db.commit()
    await db.refresh(cc)
    return cc


# ── Informe actividades ────────────────────────────────────────────────────


async def test_informe_actividades_genera_docx_valido(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    user = test_user["user"]
    content, filename = await informe_service.generar_informe_actividades_docx(
        db, user.id, cuenta.id
    )
    assert filename.endswith(".docx")
    assert filename.startswith("informe-actividades-")
    assert len(content) > 1000  # not empty
    # Parse it back to verify it's a valid docx
    doc = Document(io.BytesIO(content))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "Informe de actividades" in full_text
    assert "CTR-INF-001" in full_text or any(
        "CTR-INF-001" in cell.text for t in doc.tables for r in t.rows for cell in r.cells
    )


async def test_informe_actividades_sin_actividades_falla(
    db: AsyncSession, test_user: dict[str, Any], contrato: Contrato
) -> None:
    user = test_user["user"]
    cc = CuentaCobro(
        contrato_id=contrato.id, mes=6, anio=2024, valor=3_000_000,
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cc)
    await db.commit()
    await db.refresh(cc)
    with pytest.raises(ValidationError):
        await informe_service.generar_informe_actividades_docx(db, user.id, cc.id)


# ── Informe supervisión ────────────────────────────────────────────────────


async def test_informe_supervision_genera_docx_valido(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    user = test_user["user"]
    content, filename = await informe_service.generar_informe_supervision_docx(
        db, user.id, cuenta.id
    )
    assert filename.endswith(".docx")
    assert filename.startswith("informe-supervision-")
    doc = Document(io.BytesIO(content))
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "supervisi" in full_text.lower()
    table_texts = [
        cell.text for t in doc.tables for r in t.rows for cell in r.cells
    ]
    assert any("Carlos Supervisor" in tx for tx in table_texts)


# ── ZIP evidencias ─────────────────────────────────────────────────────────


async def test_zip_evidencias_estructura(
    db: AsyncSession, test_user: dict[str, Any], cuenta: CuentaCobro
) -> None:
    user = test_user["user"]
    content, filename = await informe_service.generar_zip_evidencias(
        db, user.id, cuenta.id
    )
    assert filename.endswith(".zip")
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        # Root readme
        assert "LEEME.txt" in names
        # One folder per obligacion (3) each with a LEEME.txt
        leemes = [n for n in names if n.endswith("LEEME.txt") and "/" in n]
        assert len(leemes) >= 3
        # Verify content of one folder
        sample = next(n for n in leemes if n.startswith("01_"))
        body = zf.read(sample).decode("utf-8")
        assert "Obligación #1" in body
        assert "Actividad realizada 1" in body


async def test_ownership_otro_usuario_falla(
    db: AsyncSession, cuenta: CuentaCobro
) -> None:
    fake_user_id = uuid.uuid4()
    with pytest.raises(ForbiddenError):
        await informe_service.generar_informe_actividades_docx(db, fake_user_id, cuenta.id)
