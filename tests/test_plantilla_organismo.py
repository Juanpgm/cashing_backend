"""Tests for the `PlantillaOrganismo` model shape (billing-resilience-templates,
slice #5, task 5.2): per-organism ingested informe template structure.

Extraction/persistence service tests (`requisito_inference_service`, tasks
5.7-5.12) and tool-wrapper tests (task 5.13) live in
`tests/services/test_plantilla_organismo_ingestion.py`.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from app.models.contrato import Contrato
from app.models.plantilla_organismo import PlantillaOrganismo
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
async def contrato(db: AsyncSession, test_user: dict[str, Any]) -> Contrato:
    user = test_user["user"]
    c = Contrato(
        usuario_id=user.id,
        numero_contrato="CTR-PLANTILLA-001",
        objeto="Servicios profesionales para pruebas de plantillas",
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


async def test_plantilla_organismo_accepts_full_shape(db: AsyncSession, contrato: Contrato) -> None:
    plantilla = PlantillaOrganismo(
        usuario_id=contrato.usuario_id,
        entidad="DAGMA",
        entidad_normalizada="dagma",
        tipo_documento="informe_actividades",
        formato="docx",
        estructura_json={
            "columnas": ["obligación", "avance del periodo"],
            "secciones": [],
            "anexo_refs": [],
            "notas": "",
        },
        fuente_documento_id=None,
    )
    db.add(plantilla)
    await db.commit()
    await db.refresh(plantilla)

    assert plantilla.id is not None
    assert plantilla.entidad == "DAGMA"
    assert plantilla.entidad_normalizada == "dagma"
    assert plantilla.tipo_documento == "informe_actividades"
    assert plantilla.formato == "docx"
    assert plantilla.estructura_json["columnas"] == ["obligación", "avance del periodo"]


async def test_plantilla_organismo_unique_per_usuario_entidad_tipo(db: AsyncSession, contrato: Contrato) -> None:
    """One structure per (usuario, organismo, tipo_documento) — re-ingesting the
    same organism/type must UPDATE, never silently accumulate duplicates."""
    from sqlalchemy.exc import IntegrityError

    p1 = PlantillaOrganismo(
        usuario_id=contrato.usuario_id,
        entidad="DAGMA",
        entidad_normalizada="dagma",
        tipo_documento="informe_actividades",
        formato="docx",
        estructura_json={"columnas": [], "secciones": [], "anexo_refs": [], "notas": ""},
    )
    db.add(p1)
    await db.commit()

    p2 = PlantillaOrganismo(
        usuario_id=contrato.usuario_id,
        entidad="DAGMA",
        entidad_normalizada="dagma",
        tipo_documento="informe_actividades",
        formato="docx",
        estructura_json={"columnas": [], "secciones": [], "anexo_refs": [], "notas": ""},
    )
    db.add(p2)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
