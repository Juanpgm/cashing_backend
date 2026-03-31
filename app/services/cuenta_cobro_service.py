"""CuentaCobro service — state machine, credit deduction, PDF generation."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, date, datetime

import structlog
from jinja2 import BaseLoader, Environment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.storage.port import StoragePort
from app.agent.tools.pdf_generator import generate_pdf_from_html
from app.core.config import settings
from app.core.exceptions import (
    AlreadyExistsError,
    ForbiddenError,
    InsufficientCreditsError,
    NotFoundError,
    ValidationError,
)
from app.models.actividad import Actividad
from app.models.contrato import Contrato
from app.models.credito import Credito, TipoCredito
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.plantilla import Plantilla, TipoPlantilla
from app.models.usuario import Usuario
from app.schemas.cuenta_cobro import (
    ActividadCreate,
    ActividadesBulkResponse,
    ActividadResponse,
    CuentaCobroCreate,
    CuentaCobroListItem,
    CuentaCobroResponse,
    GenerarPDFResponse,
    PDFUrlResponse,
)

logger = structlog.get_logger("service.cuenta_cobro")

# Valid state machine transitions
_TRANSICIONES: dict[EstadoCuentaCobro, set[EstadoCuentaCobro]] = {
    EstadoCuentaCobro.BORRADOR: {EstadoCuentaCobro.ENVIADA},
    EstadoCuentaCobro.ENVIADA: {EstadoCuentaCobro.APROBADA, EstadoCuentaCobro.RECHAZADA},
    EstadoCuentaCobro.RECHAZADA: {EstadoCuentaCobro.BORRADOR},
    EstadoCuentaCobro.APROBADA: {EstadoCuentaCobro.PAGADA},
    EstadoCuentaCobro.PAGADA: set(),
}

_MESES = [
    "Enero",
    "Febrero",
    "Marzo",
    "Abril",
    "Mayo",
    "Junio",
    "Julio",
    "Agosto",
    "Septiembre",
    "Octubre",
    "Noviembre",
    "Diciembre",
]

_DEFAULT_TEMPLATE_HTML = """\
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<style>
  body { font-family: Arial, sans-serif; font-size: 12px; margin: 40px; color: #222; }
  h1 { text-align: center; font-size: 16px; text-transform: uppercase; margin-bottom: 4px; }
  .subtitulo { text-align: center; font-size: 13px; margin-bottom: 20px; }
  table.info { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
  table.info td { padding: 5px 8px; border: 1px solid #bbb; }
  table.info td:first-child { font-weight: bold; width: 22%; background: #f5f5f5; }
  .seccion { font-weight: bold; background: #e8e8e8; padding: 6px 8px;
             margin-top: 20px; margin-bottom: 0; border: 1px solid #bbb; }
  table.acts { width: 100%; border-collapse: collapse; }
  table.acts th { background: #444; color: #fff; padding: 6px 8px; text-align: left; font-size: 11px; }
  table.acts td { padding: 6px 8px; border: 1px solid #ccc; vertical-align: top; font-size: 11px; }
  table.acts tr:nth-child(even) td { background: #fafafa; }
  .valor-total { text-align: right; font-size: 15px; font-weight: bold; margin-top: 14px; }
  .firmas { display: flex; justify-content: space-between; margin-top: 70px; }
  .firma { text-align: center; width: 220px; }
  .firma .linea { border-top: 1px solid #444; padding-top: 6px; margin-top: 4px; }
</style>
</head>
<body>
  <h1>Cuenta de Cobro</h1>
  <p class="subtitulo">Contrato de Prestación de Servicios Profesionales</p>

  <table class="info">
    <tr><td>Entidad</td><td>{{ entidad }}</td></tr>
    <tr><td>Dependencia</td><td>{{ dependencia }}</td></tr>
    <tr><td>No. Contrato</td><td>{{ numero_contrato }}</td></tr>
    <tr><td>Período</td><td>{{ mes_nombre }} de {{ anio }}</td></tr>
    <tr><td>Contratista</td><td>{{ contratista_nombre }}</td></tr>
    <tr><td>C.C.</td><td>{{ contratista_cedula }}</td></tr>
    <tr><td>Supervisor</td><td>{{ supervisor }}</td></tr>
    <tr><td colspan="2"><strong>Objeto:</strong> {{ objeto }}</td></tr>
  </table>

  <p class="seccion">Actividades realizadas en el período</p>
  <table class="acts">
    <thead>
      <tr>
        <th style="width:4%">#</th>
        <th style="width:36%">Actividad</th>
        <th style="width:40%">Justificación</th>
        <th style="width:20%">Obligación contractual</th>
      </tr>
    </thead>
    <tbody>
      {% for act in actividades %}
      <tr>
        <td>{{ loop.index }}</td>
        <td>{{ act.descripcion }}</td>
        <td>{{ act.justificacion or "—" }}</td>
        <td>{{ act.obligacion_desc or "—" }}</td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  <p class="valor-total">Valor a cobrar: $ {{ valor_formato }}</p>

  <div class="firmas">
    <div class="firma">
      <div class="linea">
        {{ contratista_nombre }}<br>
        C.C. {{ contratista_cedula }}<br>
        <em>Contratista</em>
      </div>
    </div>
    <div class="firma">
      <div class="linea">
        {{ supervisor }}<br>
        <em>Supervisor del contrato</em>
      </div>
    </div>
  </div>
</body>
</html>
"""


async def _get_cuenta_con_ownership(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> CuentaCobro:
    """Load a CuentaCobro with actividades, verifying the user owns it via the contrato."""
    result = await db.execute(
        select(CuentaCobro)
        .options(selectinload(CuentaCobro.actividades), selectinload(CuentaCobro.contrato))
        .where(CuentaCobro.id == cuenta_id, CuentaCobro.deleted_at.is_(None))
    )
    cuenta = result.scalar_one_or_none()
    if cuenta is None:
        raise NotFoundError("CuentaCobro", str(cuenta_id))
    if cuenta.contrato.usuario_id != usuario_id:
        raise ForbiddenError()
    return cuenta


async def _reload_cuenta_response(db: AsyncSession, cuenta_id: uuid.UUID) -> CuentaCobroResponse:
    """Re-query a CuentaCobro fresh from the DB with all eager-loaded relationships for serialization."""
    result = await db.execute(
        select(CuentaCobro)
        .options(selectinload(CuentaCobro.actividades), selectinload(CuentaCobro.contrato))
        .where(CuentaCobro.id == cuenta_id)
    )
    cuenta = result.scalar_one()
    return CuentaCobroResponse.model_validate(cuenta)


async def crear_cuenta_cobro(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    data: CuentaCobroCreate,
) -> CuentaCobroResponse:
    """Create a new CuentaCobro in BORRADOR state, deducting credits."""
    # Verify contrato ownership
    result = await db.execute(
        select(Contrato).where(
            Contrato.id == data.contrato_id,
            Contrato.usuario_id == usuario_id,
            Contrato.deleted_at.is_(None),
        )
    )
    contrato = result.scalar_one_or_none()
    if contrato is None:
        raise NotFoundError("Contrato", str(data.contrato_id))

    # Check credits
    user_result = await db.execute(select(Usuario).where(Usuario.id == usuario_id))
    usuario = user_result.scalar_one_or_none()
    if usuario is None:
        raise NotFoundError("Usuario", str(usuario_id))

    costo = settings.CREDITS_PER_CUENTA_COBRO
    if usuario.creditos_disponibles < costo:
        raise InsufficientCreditsError(required=costo, available=usuario.creditos_disponibles)

    # Check uniqueness (contrato_id, mes, anio)
    existing = await db.execute(
        select(CuentaCobro).where(
            CuentaCobro.contrato_id == data.contrato_id,
            CuentaCobro.mes == data.mes,
            CuentaCobro.anio == data.anio,
            CuentaCobro.deleted_at.is_(None),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise AlreadyExistsError("CuentaCobro", f"contrato={data.contrato_id} mes={data.mes}/{data.anio}")

    # Deduct credits
    usuario.creditos_disponibles -= costo
    db.add(
        Credito(
            usuario_id=usuario_id,
            cantidad=-costo,
            tipo=TipoCredito.CONSUMO,
            referencia=f"cuenta_cobro:{data.contrato_id}:{data.anio}-{data.mes:02d}",
        )
    )

    cuenta = CuentaCobro(
        contrato_id=data.contrato_id,
        mes=data.mes,
        anio=data.anio,
        valor=float(data.valor),
        estado=EstadoCuentaCobro.BORRADOR,
    )
    db.add(cuenta)
    await db.flush()

    await logger.ainfo(
        "cuenta_cobro_creada",
        cuenta_id=str(cuenta.id),
        usuario_id=str(usuario_id),
        mes=data.mes,
        anio=data.anio,
    )
    return await _reload_cuenta_response(db, cuenta.id)


async def listar_cuentas_cobro(db: AsyncSession, usuario_id: uuid.UUID) -> list[CuentaCobroListItem]:
    """List all CuentasCobro belonging to a user (via their contratos)."""
    result = await db.execute(
        select(CuentaCobro)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            Contrato.usuario_id == usuario_id,
            CuentaCobro.deleted_at.is_(None),
            Contrato.deleted_at.is_(None),
        )
        .order_by(CuentaCobro.anio.desc(), CuentaCobro.mes.desc())
    )
    cuentas = result.scalars().all()
    return [CuentaCobroListItem.model_validate(c) for c in cuentas]


async def obtener_cuenta_cobro(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> CuentaCobroResponse:
    """Get a single CuentaCobro with activities, verifying ownership."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    return CuentaCobroResponse.model_validate(cuenta)


async def agregar_actividad(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    data: ActividadCreate,
) -> ActividadResponse:
    """Add an activity to a CuentaCobro (only allowed in BORRADOR or RECHAZADA states)."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"No se pueden agregar actividades en estado '{cuenta.estado}'. Solo se permite en borrador o rechazada."
        )

    # Validate obligacion belongs to this contrato
    if data.obligacion_id is not None:
        from app.models.obligacion import Obligacion

        ob_result = await db.execute(
            select(Obligacion).where(
                Obligacion.id == data.obligacion_id,
                Obligacion.contrato_id == cuenta.contrato_id,
            )
        )
        if ob_result.scalar_one_or_none() is None:
            raise NotFoundError("Obligacion", str(data.obligacion_id))

    actividad = Actividad(
        cuenta_cobro_id=cuenta_id,
        obligacion_id=data.obligacion_id,
        descripcion=data.descripcion,
        justificacion=data.justificacion,
        fecha_realizacion=data.fecha_realizacion,
    )
    db.add(actividad)
    await db.flush()
    await db.refresh(actividad)
    return ActividadResponse.model_validate(actividad)


async def agregar_actividades_bulk(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    actividades: list[ActividadCreate],
) -> ActividadesBulkResponse:
    """Create multiple activities at once, optionally linked to obligations."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"No se pueden agregar actividades en estado '{cuenta.estado}'. Solo se permite en borrador o rechazada."
        )

    # Pre-validate all obligacion_ids belong to this contrato
    obligacion_ids = {a.obligacion_id for a in actividades if a.obligacion_id is not None}
    if obligacion_ids:
        from app.models.obligacion import Obligacion as Ob

        ob_result = await db.execute(
            select(Ob).where(
                Ob.id.in_(list(obligacion_ids)),
                Ob.contrato_id == cuenta.contrato_id,
            )
        )
        found_ids = {ob.id for ob in ob_result.scalars().all()}
        invalid = obligacion_ids - found_ids
        if invalid:
            raise NotFoundError("Obligacion", str(next(iter(invalid))))

    created: list[ActividadResponse] = []
    for data in actividades:
        act = Actividad(
            cuenta_cobro_id=cuenta_id,
            obligacion_id=data.obligacion_id,
            descripcion=data.descripcion,
            justificacion=data.justificacion,
            fecha_realizacion=data.fecha_realizacion,
        )
        db.add(act)
        await db.flush()
        await db.refresh(act)
        created.append(ActividadResponse.model_validate(act))

    await logger.ainfo(
        "actividades_bulk_creadas",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        cantidad=len(created),
    )
    return ActividadesBulkResponse(creadas=len(created), actividades=created)


_NUMBERED_LINE = re.compile(r"^\s*\d+[\.\)\-]\s+(.+)$")
_ACTIVIDAD_PARSED = re.compile(r"^ACTIVIDAD\|(.+?)\|(.+?)\|(\d+)\s*$", re.MULTILINE)


def _parse_actividades_llm(response: str, obligaciones: list) -> list[ActividadCreate]:
    """Parse pipe-delimited ACTIVIDAD lines produced by the LLM."""
    result: list[ActividadCreate] = []
    for descripcion, justificacion, ob_num_str in _ACTIVIDAD_PARSED.findall(response):
        ob_idx = int(ob_num_str) - 1
        ob_id = obligaciones[ob_idx].id if obligaciones and 0 <= ob_idx < len(obligaciones) else None
        desc = descripcion.strip()[:2000]
        just = justificacion.strip()[:3000]
        if len(desc) >= 10:
            result.append(ActividadCreate(descripcion=desc, justificacion=just, obligacion_id=ob_id))
    return result


async def agregar_actividades_desde_texto(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    texto: str,
    fecha_realizacion: date | None,
    vincular_obligaciones: bool,
) -> ActividadesBulkResponse:
    """Parse a numbered text list and create one activity per line.

    Lines that start with a number (1. / 1) / 1-) are extracted.
    If vincular_obligaciones=True and the contract has obligations,
    each activity is linked by position (line 1 → obligacion[0], etc.).
    """
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"No se pueden agregar actividades en estado '{cuenta.estado}'. Solo se permite en borrador o rechazada."
        )

    # Parse numbered lines
    lineas = [_NUMBERED_LINE.match(line) for line in texto.splitlines()]
    descripciones = [m.group(1).strip() for m in lineas if m]

    if not descripciones:
        raise ValidationError(
            "No se encontraron actividades en el texto. "
            "Cada actividad debe empezar con un número seguido de punto, paréntesis o guion. "
            "Ejemplo: '1. Elaboré el informe mensual'"
        )

    # Load obligations sorted by order for auto-linking
    obligaciones: list = []
    if vincular_obligaciones:
        from app.models.obligacion import Obligacion as Ob

        ob_result = await db.execute(
            select(Ob)
            .where(Ob.contrato_id == cuenta.contrato_id)
            .order_by(Ob.orden)
        )
        obligaciones = ob_result.scalars().all()

    created: list[ActividadResponse] = []
    for i, desc in enumerate(descripciones):
        if len(desc) < 10:
            raise ValidationError(
                f"La actividad {i + 1} es demasiado corta (mínimo 10 caracteres): '{desc}'"
            )

        ob_id = obligaciones[i].id if (vincular_obligaciones and i < len(obligaciones)) else None

        act = Actividad(
            cuenta_cobro_id=cuenta_id,
            obligacion_id=ob_id,
            descripcion=desc,
            fecha_realizacion=fecha_realizacion,
        )
        db.add(act)
        await db.flush()
        await db.refresh(act)
        created.append(ActividadResponse.model_validate(act))

    await logger.ainfo(
        "actividades_desde_texto_creadas",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        cantidad=len(created),
        vinculadas=vincular_obligaciones,
    )
    return ActividadesBulkResponse(creadas=len(created), actividades=created)


async def generar_actividades_agente(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> ActividadesBulkResponse:
    """Use the LLM to generate and persist activities from contract obligations and document text.

    Requires the contract to have at least one obligation registered OR a contract document
    uploaded. If neither is available, raises ValidationError pointing to /desde-texto.
    """
    from app.adapters.llm import get_llm
    from app.agent.prompts.actividades import ACTIVIDADES_GENERATION_PROMPT
    from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
    from app.models.obligacion import Obligacion as Ob
    from app.schemas.agent import LLMMessage

    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"No se pueden generar actividades en estado '{cuenta.estado}'. "
            "Solo se permite en borrador o rechazada."
        )

    contrato = cuenta.contrato

    # Load obligations sorted by order
    ob_result = await db.execute(
        select(Ob).where(Ob.contrato_id == contrato.id).order_by(Ob.orden)
    )
    obligaciones = ob_result.scalars().all()

    # Load contract document text if available
    doc_result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.contrato_id == contrato.id,
            DocumentoFuente.usuario_id == usuario_id,
            DocumentoFuente.tipo == TipoDocumentoFuente.CONTRATO,
        )
    )
    docs = doc_result.scalars().all()
    texto_contrato = next((d.texto_extraido for d in docs if d.texto_extraido), None)

    if not obligaciones and not texto_contrato:
        raise ValidationError(
            "El contrato no tiene obligaciones registradas ni documento de contrato cargado. "
            "Registre las obligaciones en POST /contratos/{id}/obligaciones, "
            "suba el documento en POST /documentos/upload, "
            "o ingrese las actividades manualmente en POST /cuentas-cobro/{id}/actividades/desde-texto."
        )

    # Build context for the LLM
    obligaciones_str = "\n".join(
        f"{i + 1}. [{ob.tipo.value.upper()}] {ob.descripcion}"
        for i, ob in enumerate(obligaciones)
    ) or "(no registradas — inferir del objeto del contrato)"

    user_content = (
        f"Contrato N° {contrato.numero_contrato}\n"
        f"Entidad: {contrato.entidad or '—'}\n"
        f"Objeto: {contrato.objeto}\n"
        f"Período a facturar: {_MESES[cuenta.mes - 1]} {cuenta.anio}\n\n"
        f"OBLIGACIONES CONTRACTUALES:\n{obligaciones_str}\n"
    )
    if texto_contrato:
        user_content += f"\nTEXTO DEL CONTRATO (fragmento):\n{texto_contrato[:3000]}"

    llm = get_llm()
    messages = [
        LLMMessage(role="system", content=ACTIVIDADES_GENERATION_PROMPT),
        LLMMessage(role="user", content=user_content),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.3, max_tokens=4096)
    except Exception as exc:
        await logger.aerror("generar_actividades_error", cuenta_id=str(cuenta_id), error=str(exc))
        raise ValidationError(f"Error al generar actividades con el agente: {exc}") from exc

    actividades_data = _parse_actividades_llm(resp.content, list(obligaciones))

    if not actividades_data:
        raise ValidationError(
            "El agente no pudo generar actividades con el formato esperado. "
            "Use POST /actividades/desde-texto para ingresar las actividades manualmente."
        )

    created: list[ActividadResponse] = []
    for data in actividades_data:
        act = Actividad(
            cuenta_cobro_id=cuenta_id,
            obligacion_id=data.obligacion_id,
            descripcion=data.descripcion,
            justificacion=data.justificacion,
            fecha_realizacion=data.fecha_realizacion,
        )
        db.add(act)
        await db.flush()
        await db.refresh(act)
        created.append(ActividadResponse.model_validate(act))

    await logger.ainfo(
        "actividades_generadas_agente",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        cantidad=len(created),
        tokens=resp.total_tokens,
    )
    return ActividadesBulkResponse(creadas=len(created), actividades=created)


async def cambiar_estado(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    nuevo_estado: EstadoCuentaCobro,
) -> CuentaCobroResponse:
    """Transition a CuentaCobro to a new state, enforcing the state machine."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    estado_actual = cuenta.estado

    if nuevo_estado not in _TRANSICIONES.get(estado_actual, set()):
        validas = ", ".join(e.value for e in _TRANSICIONES.get(estado_actual, set())) or "ninguna"
        raise ValidationError(
            f"Transición inválida: {estado_actual} → {nuevo_estado}. "
            f"Transiciones válidas desde '{estado_actual}': {validas}."
        )

    cuenta.estado = nuevo_estado
    if nuevo_estado == EstadoCuentaCobro.ENVIADA:
        cuenta.fecha_envio = datetime.now(UTC)

    await db.flush()

    await logger.ainfo(
        "cuenta_cobro_estado_cambiado",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        estado_anterior=estado_actual,
        estado_nuevo=nuevo_estado,
    )
    return await _reload_cuenta_response(db, cuenta_id)


async def generar_pdf(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    storage: StoragePort,
) -> GenerarPDFResponse:
    """Render a CuentaCobro to PDF, upload to storage, and return a presigned URL."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    # Load contrato + user for template context
    contrato = cuenta.contrato
    user_result = await db.execute(select(Usuario).where(Usuario.id == usuario_id))
    usuario = user_result.scalar_one_or_none()
    if usuario is None:
        raise NotFoundError("Usuario", str(usuario_id))

    # Load obligaciones descriptions for activities
    obligacion_desc: dict[uuid.UUID, str] = {}
    if contrato.obligaciones:
        for ob in contrato.obligaciones:
            obligacion_desc[ob.id] = ob.descripcion

    # Build template context
    actividades_ctx = [
        {
            "descripcion": act.descripcion,
            "justificacion": act.justificacion,
            "obligacion_desc": obligacion_desc.get(act.obligacion_id, "") if act.obligacion_id else "",
        }
        for act in cuenta.actividades
    ]

    valor_num = float(cuenta.valor)
    context = {
        "entidad": contrato.entidad or "",
        "dependencia": contrato.dependencia or "",
        "numero_contrato": contrato.numero_contrato,
        "mes_nombre": _MESES[cuenta.mes - 1],
        "anio": cuenta.anio,
        "contratista_nombre": usuario.nombre,
        "contratista_cedula": usuario.cedula or "—",
        "supervisor": contrato.supervisor_nombre or "—",
        "objeto": contrato.objeto,
        "actividades": actividades_ctx,
        "valor_formato": f"{valor_num:,.2f}",
    }

    # Resolve template: user's custom template or built-in default
    tmpl_result = await db.execute(
        select(Plantilla)
        .where(
            Plantilla.tipo == TipoPlantilla.CUENTA_COBRO,
            Plantilla.activa.is_(True),
            (Plantilla.usuario_id == usuario_id) | (Plantilla.usuario_id.is_(None)),
        )
        .order_by(Plantilla.usuario_id.desc().nullslast())  # user's own first
    )
    plantilla = tmpl_result.scalars().first()
    template_html = plantilla.contenido_html if plantilla else _DEFAULT_TEMPLATE_HTML

    # Render HTML → PDF
    env = Environment(loader=BaseLoader(), autoescape=True)
    html = env.from_string(template_html).render(**context)
    pdf_bytes = generate_pdf_from_html(html)

    # Upload to storage
    storage_key = f"pdfs/{usuario_id}/{cuenta_id}.pdf"
    await storage.upload(storage_key, pdf_bytes, content_type="application/pdf")

    # Persist key on the model
    cuenta.pdf_storage_key = storage_key
    await db.flush()

    presigned = await storage.presigned_url(storage_key, expires_in=3600)

    await logger.ainfo(
        "cuenta_cobro_pdf_generado",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        storage_key=storage_key,
    )
    return GenerarPDFResponse(pdf_url=presigned, pdf_storage_key=storage_key)


async def obtener_url_pdf(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    storage: StoragePort,
) -> PDFUrlResponse:
    """Return a fresh presigned URL for the stored PDF."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if not cuenta.pdf_storage_key:
        raise ValidationError("El PDF no ha sido generado aún. Use POST /generar-pdf primero.")

    presigned = await storage.presigned_url(cuenta.pdf_storage_key, expires_in=3600)
    return PDFUrlResponse(pdf_url=presigned)


async def eliminar_cuenta_cobro(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> None:
    """Soft-delete a CuentaCobro. Only allowed when in BORRADOR state."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado != EstadoCuentaCobro.BORRADOR:
        raise ValidationError(
            f"Solo se pueden eliminar cuentas en estado 'borrador'. Estado actual: '{cuenta.estado}'."
        )

    cuenta.deleted_at = datetime.now(UTC)
    await db.flush()
    await logger.ainfo("cuenta_cobro_eliminada", cuenta_id=str(cuenta_id), usuario_id=str(usuario_id))
