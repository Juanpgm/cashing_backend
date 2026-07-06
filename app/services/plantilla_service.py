"""Plantilla service — CRUD for HTML document templates."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.plantilla import Plantilla, TipoPlantilla
from app.schemas.plantilla import (
    PlantillaCreate,
    PlantillaRenderRequest,
    PlantillaRenderResponse,
    PlantillaResponse,
    PlantillaUpdate,
)

logger = structlog.get_logger("service.plantilla")


async def _get_plantilla_owned(
    db: AsyncSession, plantilla_id: uuid.UUID, usuario_id: uuid.UUID
) -> Plantilla:
    result = await db.execute(
        select(Plantilla).where(
            Plantilla.id == plantilla_id,
            Plantilla.usuario_id == usuario_id,
            Plantilla.activa.is_(True),
        )
    )
    p = result.scalar_one_or_none()
    if p is None:
        raise NotFoundError("Plantilla", str(plantilla_id))
    return p


async def crear_plantilla(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    data: PlantillaCreate,
) -> PlantillaResponse:
    plantilla = Plantilla(
        usuario_id=usuario_id,
        nombre=data.nombre,
        tipo=data.tipo,
        contenido_html=data.contenido_html,
        activa=True,
    )
    db.add(plantilla)
    await db.commit()
    await db.refresh(plantilla)
    logger.info("plantilla_created", id=str(plantilla.id), tipo=plantilla.tipo)
    return PlantillaResponse.model_validate(plantilla)


async def listar_plantillas(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    tipo: TipoPlantilla | None = None,
) -> list[PlantillaResponse]:
    q = select(Plantilla).where(
        Plantilla.usuario_id == usuario_id,
        Plantilla.activa.is_(True),
    )
    if tipo is not None:
        q = q.where(Plantilla.tipo == tipo)
    q = q.order_by(Plantilla.created_at.desc())
    result = await db.execute(q)
    return [PlantillaResponse.model_validate(p) for p in result.scalars().all()]


async def obtener_plantilla(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    plantilla_id: uuid.UUID,
) -> PlantillaResponse:
    p = await _get_plantilla_owned(db, plantilla_id, usuario_id)
    return PlantillaResponse.model_validate(p)


async def actualizar_plantilla(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    plantilla_id: uuid.UUID,
    data: PlantillaUpdate,
) -> PlantillaResponse:
    p = await _get_plantilla_owned(db, plantilla_id, usuario_id)
    for field, value in data.model_dump(exclude_none=True).items():
        setattr(p, field, value)
    await db.commit()
    await db.refresh(p)
    return PlantillaResponse.model_validate(p)


async def eliminar_plantilla(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    plantilla_id: uuid.UUID,
) -> None:
    p = await _get_plantilla_owned(db, plantilla_id, usuario_id)
    p.activa = False
    await db.commit()
    logger.info("plantilla_deactivated", id=str(plantilla_id))


async def renderizar_plantilla(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    plantilla_id: uuid.UUID,
    req: PlantillaRenderRequest,
) -> PlantillaRenderResponse:
    """Render a template with the provided data, optionally generate PDF."""
    from jinja2 import BaseLoader, Environment

    from app.agent.tools.pdf_generator import generate_pdf_from_html

    p = await _get_plantilla_owned(db, plantilla_id, usuario_id)
    env = Environment(loader=BaseLoader(), autoescape=True)
    tmpl = env.from_string(p.contenido_html)
    html = tmpl.render(**req.data)

    pdf_b64: str | None = None
    try:
        import base64
        pdf_bytes = generate_pdf_from_html(html)
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
    except Exception:
        # WeasyPrint not available in all envs — return HTML only
        pass

    return PlantillaRenderResponse(html=html, pdf_b64=pdf_b64)
