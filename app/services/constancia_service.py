"""Constancia service — generate a PDF certificate of contractual obligation fulfillment.

Produces a single-page PDF (via WeasyPrint) that certifies the contractor
delivered the required activities and documents for a billing period.
The PDF includes visual signature blocks for contractor and supervisor;
cryptographic signing (PAdES/pyhanko) is not applied in this version.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload  # noqa: F401 — used in CuentaCobro options

from app.agent.tools.pdf_generator import generate_pdf_from_template
from app.core.exceptions import ForbiddenError, NotFoundError
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro
from app.models.documento_cuenta_cobro import DocumentoCuentaCobro, EstadoRequisito
from app.models.obligacion import Obligacion
from app.models.requisito_documento import RequisitoDocumento
from app.models.usuario import Usuario

logger = structlog.get_logger("service.constancia")

_TEMPLATE_PATH = Path(__file__).parent.parent / "templates" / "constancia.html"

_MESES = [
    "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

_ESTADO_LABELS: dict[str, str] = {
    EstadoRequisito.PENDIENTE.value: "Pendiente",
    EstadoRequisito.DETECTADO.value: "Detectado automáticamente",
    EstadoRequisito.CARGADO.value: "Documento cargado",
    EstadoRequisito.CUMPLIDO_MANUAL.value: "Marcado cumplido",
    EstadoRequisito.NO_APLICA.value: "No aplica",
}

_ESTADOS_CUMPLIDOS = {
    EstadoRequisito.CARGADO.value,
    EstadoRequisito.DETECTADO.value,
    EstadoRequisito.CUMPLIDO_MANUAL.value,
}


def _periodo_str(cuenta: CuentaCobro) -> str:
    return f"{_MESES[cuenta.mes - 1]} de {cuenta.anio}"


def _formato_fecha(d: date | None) -> str:
    return d.strftime("%d/%m/%Y") if d else "—"


def _formato_valor(v: float | None) -> str:
    if v is None:
        return "—"
    return f"$ {float(v):,.2f}"


async def generar_constancia_pdf(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> tuple[bytes, str]:
    """Generate a PDF constancia for the given CuentaCobro.

    Returns (pdf_bytes, filename).
    Raises NotFoundError if the cuenta does not exist.
    Raises ForbiddenError if usuario_id does not own the related contrato.
    """
    # ── 1. Load CuentaCobro + related data ───────────────────────────────────
    result = await db.execute(
        select(CuentaCobro)
        .options(
            selectinload(CuentaCobro.contrato).selectinload(Contrato.obligaciones),
            selectinload(CuentaCobro.actividades),
        )
        .where(CuentaCobro.id == cuenta_id, CuentaCobro.deleted_at.is_(None))
    )
    cuenta = result.scalar_one_or_none()
    if cuenta is None:
        raise NotFoundError("CuentaCobro", str(cuenta_id))
    if cuenta.contrato.usuario_id != usuario_id:
        raise ForbiddenError()

    contrato = cuenta.contrato
    obligaciones_by_id: dict[uuid.UUID, Obligacion] = {
        ob.id: ob for ob in contrato.obligaciones
    }
    actividades = sorted(cuenta.actividades, key=lambda a: a.created_at)

    # ── 2. Load Usuario ───────────────────────────────────────────────────────
    user_result = await db.execute(select(Usuario).where(Usuario.id == usuario_id))
    usuario = user_result.scalar_one_or_none()
    if usuario is None:
        raise NotFoundError("Usuario", str(usuario_id))

    # ── 3. Load checklist items ───────────────────────────────────────────────
    checklist_result = await db.execute(
        select(DocumentoCuentaCobro)
        .where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id)
        .order_by(DocumentoCuentaCobro.requisito_codigo)
    )
    checklist_rows = list(checklist_result.scalars().all())

    # Load RequisitoDocumento catalog for labels
    req_codigos = [row.requisito_codigo for row in checklist_rows]
    requisitos_by_codigo: dict[str, RequisitoDocumento] = {}
    if req_codigos:
        req_result = await db.execute(
            select(RequisitoDocumento).where(RequisitoDocumento.codigo.in_(req_codigos))
        )
        requisitos_by_codigo = {r.codigo: r for r in req_result.scalars().all()}

    # ── 4. Build template context ─────────────────────────────────────────────
    checklist_items = [
        {
            "etiqueta": (
                requisitos_by_codigo[row.requisito_codigo].etiqueta
                if row.requisito_codigo in requisitos_by_codigo
                else row.requisito_codigo
            ),
            "estado": row.estado.value if row.estado else EstadoRequisito.PENDIENTE.value,
            "estado_label": _ESTADO_LABELS.get(
                row.estado.value if row.estado else EstadoRequisito.PENDIENTE.value,
                "Pendiente",
            ),
            "cumplido": (row.estado.value if row.estado else "") in _ESTADOS_CUMPLIDOS,
            "no_aplica": row.estado == EstadoRequisito.NO_APLICA,
            "observaciones": row.observaciones,
        }
        for row in checklist_rows
    ]

    actividades_ctx = [
        {
            "descripcion": act.descripcion,
            "obligacion_descripcion": (
                obligaciones_by_id[act.obligacion_id].descripcion
                if act.obligacion_id and act.obligacion_id in obligaciones_by_id
                else None
            ),
            "fecha_realizacion": _formato_fecha(act.fecha_realizacion),
        }
        for act in actividades
    ]

    today = date.today()
    context = {
        "contrato_numero": contrato.numero_contrato,
        "entidad": contrato.entidad or "—",
        "dependencia": contrato.dependencia or "—",
        "objeto": contrato.objeto,
        "contratista_nombre": usuario.nombre,
        "contratista_cedula": usuario.cedula or "—",
        "supervisor_nombre": contrato.supervisor_nombre or "—",
        "periodo": _periodo_str(cuenta),
        "valor_periodo": _formato_valor(float(cuenta.valor) if cuenta.valor else None),
        "fecha_generacion": today.strftime("%d/%m/%Y"),
        "checklist_items": checklist_items,
        "actividades": actividades_ctx,
    }

    # ── 5. Render template → PDF ──────────────────────────────────────────────
    template_html = _TEMPLATE_PATH.read_text(encoding="utf-8")
    pdf_bytes = generate_pdf_from_template(template_html, context)

    filename = (
        f"constancia-{contrato.numero_contrato}-"
        f"{cuenta.anio}-{cuenta.mes:02d}.pdf"
    )

    await logger.ainfo(
        "constancia_generada",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        size=len(pdf_bytes),
        checklist_items=len(checklist_items),
        actividades=len(actividades_ctx),
    )
    return pdf_bytes, filename
