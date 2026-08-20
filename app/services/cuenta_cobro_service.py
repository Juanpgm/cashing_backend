"""CuentaCobro service — state machine, credit deduction, PDF generation."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any

import structlog
from jinja2 import BaseLoader, Environment
from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.adapters.storage.port import StoragePort
from app.agent.tools.pdf_generator import generate_pdf_from_html
from app.core.config import settings
from app.core.exceptions import (
    CHECKLIST_INCOMPLETE,
    COHERENCE_CHECK_FAILED,
    CUENTA_MES_DUPLICADA,
    CUOTA_NUMERO_CONFLICT,
    CUOTA_POSITION_CONFLICT,
    AlreadyExistsError,
    ForbiddenError,
    InsufficientCreditsError,
    NotFoundError,
    ValidationError,
)
from app.models.actividad import Actividad, JustificacionOrigen
from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.clasificacion_job import ClasificacionEvidenciasJob
from app.models.contrato import Contrato
from app.models.conversacion import Conversacion
from app.models.credito import Credito, TipoCredito
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro, PosicionCuota
from app.models.documento_cuenta_cobro import (
    DocumentoChecklistCandidato,
    DocumentoCuentaCobro,
    DocumentoRequisitoVinculo,
)
from app.models.documento_fuente import DocumentoFuente
from app.models.evidencia import Evidencia
from app.models.evidencia_obligacion import EvidenciaObligacion
from app.models.plantilla import Plantilla, TipoPlantilla
from app.models.requisito_cuenta import RequisitoCuenta
from app.models.usuario import Usuario
from app.schemas.coherence import FindingOut
from app.schemas.cuenta_cobro import (
    ActividadCreate,
    ActividadesBulkResponse,
    ActividadResponse,
    CuentaCobroCreate,
    CuentaCobroListItem,
    CuentaCobroResponse,
    CuentaCobroUpdate,
    GenerarPDFResponse,
    PDFUrlResponse,
)
from app.services import checklist_service, coherence_validator_service
from app.services.coherence_validator_service import Severity

if TYPE_CHECKING:
    from app.models.obligacion import Obligacion

logger = structlog.get_logger("service.cuenta_cobro")

# Per-obligación justification-context budget (justification-context: Per-
# obligación context not bound to the old flat caps) — deliberately DIFFERENT
# from the legacy flat-blob cap (10 rows / 1500 chars, still used verbatim by
# `_contexto_evidencias_flat` as the zero-links fallback below).
_EVIDENCIAS_POR_OBLIGACION_MAX = 4
_EVIDENCIA_CHARS_POR_OBLIGACION_MAX = 1200

# Valid state machine transitions.
# RECHAZADA -> ENVIADA lets a resubmission (POST /radicar) go straight back to
# ENVIADA without forcing a detour through BORRADOR first.
# ENVIADA -> BORRADOR lets a user reopen a submitted cuenta for edits ("Reabrir
# y editar") before it has been aprobada/rechazada.
_TRANSICIONES: dict[EstadoCuentaCobro, set[EstadoCuentaCobro]] = {
    EstadoCuentaCobro.BORRADOR: {EstadoCuentaCobro.ENVIADA},
    EstadoCuentaCobro.ENVIADA: {
        EstadoCuentaCobro.APROBADA,
        EstadoCuentaCobro.RECHAZADA,
        EstadoCuentaCobro.BORRADOR,
    },
    EstadoCuentaCobro.RECHAZADA: {EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.ENVIADA},
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


async def _numero_cuota_siguiente(db: AsyncSession, contrato_id: uuid.UUID) -> int:
    """1-based `numero_cuota` for a NEW cuota of this contrato — count of active
    (non-deleted) existing cuotas + 1 (billing-resilience-templates slice #3, D3).

    Derived and persisted once at creation time — never re-derived at read time.
    """
    result = await db.execute(
        select(func.count())
        .select_from(CuentaCobro)
        .where(CuentaCobro.contrato_id == contrato_id, CuentaCobro.deleted_at.is_(None))
    )
    return (result.scalar_one() or 0) + 1


async def _verificar_conflicto_posicion(
    db: AsyncSession,
    contrato_id: uuid.UUID,
    *,
    informe_final: bool = False,
    posicion_primera: bool = False,
    excluir_cuenta_id: uuid.UUID | None = None,
) -> None:
    """Reject a write that would produce an inconsistent cuota position for a
    contrato: a second cuota with `informe_final=true`, or a second cuota with
    `posicion=primera` (billing-resilience-templates slice #3, D3). Raises
    `ValidationError(code=CUOTA_POSITION_CONFLICT)` when an existing, active cuota
    of the SAME contrato already claims the position being requested.
    """
    condiciones: list[ColumnElement[bool]] = []
    if informe_final:
        condiciones.append(CuentaCobro.informe_final.is_(True))
    if posicion_primera:
        condiciones.append(CuentaCobro.posicion == PosicionCuota.PRIMERA)
    if not condiciones:
        return

    stmt = select(CuentaCobro.id).where(
        CuentaCobro.contrato_id == contrato_id,
        CuentaCobro.deleted_at.is_(None),
        or_(*condiciones),
    )
    if excluir_cuenta_id is not None:
        stmt = stmt.where(CuentaCobro.id != excluir_cuenta_id)

    existente = await db.execute(stmt)
    if existente.scalar_one_or_none() is not None:
        detalle = "informe_final=true" if informe_final else "posicion=primera"
        raise ValidationError(
            f"Ya existe otra cuota de este contrato con {detalle}. Solo puede haber una.",
            code=CUOTA_POSITION_CONFLICT,
        )


async def _verificar_numero_cuota_libre(
    db: AsyncSession,
    contrato_id: uuid.UUID,
    numero_cuota: int,
    *,
    excluir_cuenta_id: uuid.UUID | None = None,
) -> None:
    """Reject an explicit `numero_cuota` override that collides with another
    active (non-deleted) cuota of the SAME contrato (cuota-numero-explicito).
    Raises `ValidationError(code=CUOTA_NUMERO_CONFLICT)` on collision.
    """
    stmt = select(CuentaCobro.id).where(
        CuentaCobro.contrato_id == contrato_id,
        CuentaCobro.deleted_at.is_(None),
        CuentaCobro.numero_cuota == numero_cuota,
    )
    if excluir_cuenta_id is not None:
        stmt = stmt.where(CuentaCobro.id != excluir_cuenta_id)

    existente = await db.execute(stmt)
    if existente.scalar_one_or_none() is not None:
        raise ValidationError(
            f"Ya existe otra cuota de este contrato con numero_cuota={numero_cuota}.",
            code=CUOTA_NUMERO_CONFLICT,
        )


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

    # Resolve valor: explicit value wins, otherwise fall back to the contrato's
    # monthly value (B.2). Fail fast if neither is available.
    valor = data.valor if data.valor is not None else contrato.valor_mensual
    if not valor:
        raise ValidationError("valor is required when the contract has no valor_mensual configured.")

    # Check credits
    user_result = await db.execute(select(Usuario).where(Usuario.id == usuario_id))
    usuario = user_result.scalar_one_or_none()
    if usuario is None:
        raise NotFoundError("Usuario", str(usuario_id))

    costo = settings.CREDITS_PER_CUENTA_COBRO
    if usuario.creditos_disponibles < costo:
        raise InsufficientCreditsError(required=costo, available=usuario.creditos_disponibles)

    # Check uniqueness (contrato_id, mes, anio) — active records only
    existing = await db.execute(
        select(CuentaCobro).where(
            CuentaCobro.contrato_id == data.contrato_id,
            CuentaCobro.mes == data.mes,
            CuentaCobro.anio == data.anio,
            CuentaCobro.deleted_at.is_(None),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise AlreadyExistsError(
            "CuentaCobro", f"contrato={data.contrato_id} mes={data.mes}/{data.anio}", code=CUENTA_MES_DUPLICADA
        )

    # The DB unique index covers ALL rows including soft-deleted ones.
    # Hard-delete any tombstone that would block the INSERT.
    await db.execute(
        sa_delete(CuentaCobro).where(
            CuentaCobro.contrato_id == data.contrato_id,
            CuentaCobro.mes == data.mes,
            CuentaCobro.anio == data.anio,
            CuentaCobro.deleted_at.is_not(None),
        )
    )

    # Cuota position (billing-resilience-templates slice #3, D3): derive numero_cuota
    # from the count of existing active cuotas, promote to posicion=primera when it's
    # the contrato's first. informe_final is NEVER inferred — only ever set explicitly
    # via `data.informe_final`. Both invariants are checked BEFORE the insert.
    if data.numero_cuota is not None:
        await _verificar_numero_cuota_libre(db, data.contrato_id, data.numero_cuota)
        numero_cuota = data.numero_cuota
    else:
        numero_cuota = await _numero_cuota_siguiente(db, data.contrato_id)
    posicion = PosicionCuota.PRIMERA if numero_cuota == 1 else PosicionCuota.RECURRENTE
    if posicion == PosicionCuota.PRIMERA:
        await _verificar_conflicto_posicion(db, data.contrato_id, posicion_primera=True)
    if data.informe_final:
        await _verificar_conflicto_posicion(db, data.contrato_id, informe_final=True)

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
        valor=float(valor),
        estado=EstadoCuentaCobro.BORRADOR,
        numero_cuota=numero_cuota,
        posicion=posicion,
        informe_final=data.informe_final,
        fecha_transaccion=data.fecha_transaccion,
    )
    db.add(cuenta)
    await db.flush()

    # The checklist is NOT materialised here: the cuenta nace con requisitos_modo
    # = NULL so the post-creation gate can ask the user how to build the checklist
    # (standard / inferred from a document / inferred from pasted text). Once the
    # user confirms a mode via POST /cuentas-cobro/{id}/requisitos, the checklist
    # is materialised (and SECOP detection runs from /refresh-secop on demand).

    await logger.ainfo(
        "cuenta_cobro_creada",
        cuenta_id=str(cuenta.id),
        usuario_id=str(usuario_id),
        mes=data.mes,
        anio=data.anio,
    )
    return await _reload_cuenta_response(db, cuenta.id)


async def listar_cuentas_cobro(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID | None = None,
) -> list[CuentaCobroListItem]:
    """List CuentasCobro belonging to a user (via their contratos).

    When ``contrato_id`` is provided, the result is restricted to that contract,
    so a contract only ever sees its own cuentas — never those of a sibling
    contract of the same user. Ownership is still enforced via the join on
    ``Contrato.usuario_id``.
    """
    stmt = (
        select(CuentaCobro)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            Contrato.usuario_id == usuario_id,
            CuentaCobro.deleted_at.is_(None),
            Contrato.deleted_at.is_(None),
        )
        .order_by(CuentaCobro.anio.desc(), CuentaCobro.mes.desc())
    )
    if contrato_id is not None:
        stmt = stmt.where(CuentaCobro.contrato_id == contrato_id)
    result = await db.execute(stmt)
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
    """Parse pipe-delimited ACTIVIDAD lines produced by the LLM.

    Post-parse guard: if descripcion and justificacion are near-identical (the model
    ignored the FORBID rules in ACTIVIDADES_GENERATION_PROMPT), the justificacion is
    reset to "" rather than persisting two copies of the same text — the user fills
    it in manually instead of getting a duplicated, low-quality pair.
    """
    from app.agent.prompts.actividad_generation import is_near_identical

    result: list[ActividadCreate] = []
    for descripcion, justificacion, ob_num_str in _ACTIVIDAD_PARSED.findall(response):
        ob_idx = int(ob_num_str) - 1
        ob_id = obligaciones[ob_idx].id if obligaciones and 0 <= ob_idx < len(obligaciones) else None
        desc = descripcion.strip()[:2000]
        just = justificacion.strip()[:3000]
        if is_near_identical(desc, just):
            just = ""
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

        ob_result = await db.execute(select(Ob).where(Ob.contrato_id == cuenta.contrato_id).order_by(Ob.orden))
        obligaciones = ob_result.scalars().all()

    created: list[ActividadResponse] = []
    for i, desc in enumerate(descripciones):
        if len(desc) < 10:
            raise ValidationError(f"La actividad {i + 1} es demasiado corta (mínimo 10 caracteres): '{desc}'")

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


async def _contexto_evidencias_flat(db: AsyncSession, cuenta_id: uuid.UUID) -> str:
    """Legacy top-10/1500-char flat query (unchanged behavior) — kept ONLY as the
    fallback for cuentas with ZERO confirmed evidencia<->obligación links at all
    (justification-context: Flat blob as fallback only when no links exist)."""
    from app.models.evidencia import Evidencia

    evidencias_result = await db.execute(
        select(Evidencia.nombre_archivo, Evidencia.texto_extraido)
        .join(Actividad, Evidencia.actividad_id == Actividad.id)
        .where(
            Actividad.cuenta_cobro_id == cuenta_id,
            Evidencia.storage_key.is_not(None),
            Evidencia.texto_extraido.is_not(None),
        )
        .order_by(Evidencia.created_at.asc())
        .limit(10)
    )
    return "\n\n".join(
        f"### {nombre}\n{(texto or '')[:1500]}" for nombre, texto in evidencias_result.all() if (texto or "").strip()
    )


async def _contexto_evidencias_por_obligacion(
    db: AsyncSession, cuenta_id: uuid.UUID, obligaciones: Sequence[Obligacion]
) -> str:
    """Build the 'EVIDENCIAS SUBIDAS' prompt block grounded in CONFIRMED
    Evidencia<->Obligacion links, one section per obligación (justification-
    context: Per-obligación context consumption) — replaces the flat
    cross-obligación query previously at this call site.

    Caps at `_EVIDENCIAS_POR_OBLIGACION_MAX` evidencias / `_EVIDENCIA_CHARS_POR_
    OBLIGACION_MAX` chars EACH, a deliberately different budget from the legacy
    flat cap since it's now scoped per obligación (justification-context: not
    bound to the old flat caps).

    Falls back to `_contexto_evidencias_flat` ONLY when the cuenta has ZERO
    confirmed links at all — an obligación that individually lacks a confirmed
    link simply contributes no section, rather than repeating the whole
    cuenta's flat evidence blob for every unlinked obligación (which would
    reintroduce the cross-obligación leakage this rewire removes).
    """
    from app.models.evidencia import Evidencia
    from app.models.evidencia_obligacion import EstadoEnlace, EvidenciaObligacion

    obligacion_ids = [ob.id for ob in obligaciones]
    if not obligacion_ids:
        return await _contexto_evidencias_flat(db, cuenta_id)

    links_result = await db.execute(
        select(EvidenciaObligacion.obligacion_id, Evidencia.nombre_archivo, Evidencia.texto_extraido)
        .join(Evidencia, Evidencia.id == EvidenciaObligacion.evidencia_id)
        .where(
            EvidenciaObligacion.status == EstadoEnlace.CONFIRMED.value,
            EvidenciaObligacion.obligacion_id.in_(obligacion_ids),
            Evidencia.texto_extraido.is_not(None),
        )
        .order_by(Evidencia.created_at.asc())
    )
    por_obligacion: dict[uuid.UUID, list[tuple[str, str]]] = {}
    for obligacion_id, nombre, texto in links_result.all():
        if not (texto or "").strip():
            continue
        por_obligacion.setdefault(obligacion_id, []).append((nombre, texto))

    if not por_obligacion:
        return await _contexto_evidencias_flat(db, cuenta_id)

    secciones: list[str] = []
    for ob in obligaciones:
        archivos = por_obligacion.get(ob.id)
        if not archivos:
            continue
        cuerpo = "\n\n".join(
            f"### {nombre}\n{texto[:_EVIDENCIA_CHARS_POR_OBLIGACION_MAX]}"
            for nombre, texto in archivos[:_EVIDENCIAS_POR_OBLIGACION_MAX]
        )
        secciones.append(f"[Obligación: {ob.descripcion[:120]}]\n{cuerpo}")

    return "\n\n".join(secciones)


async def generar_actividades_agente(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    periodo_inicio: date | None = None,
    periodo_fin: date | None = None,
) -> ActividadesBulkResponse:
    """Use the LLM to generate and persist activities from contract obligations and document text.

    Requires the contract to have at least one obligation registered OR a contract document
    uploaded. If neither is available, raises ValidationError pointing to /desde-texto.

    `periodo_inicio`/`periodo_fin` are optional month-scoping bounds (see
    `app.core.month_scoping.calcular_ventana_mes`): when both are `None` (the default),
    behavior is byte-identical to before this parameter existed. When both are set, they
    are surfaced to the LLM as the bounded period to justify — this never changes which
    obligations/documents are loaded, only what the prompt tells the agent about the window.
    """
    from app.adapters.llm import get_llm
    from app.agent.prompts.actividades import ACTIVIDADES_GENERATION_PROMPT
    from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
    from app.models.obligacion import Obligacion as Ob
    from app.schemas.agent import LLMMessage

    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"No se pueden generar actividades en estado '{cuenta.estado}'. Solo se permite en borrador o rechazada."
        )

    contrato = cuenta.contrato

    # Load obligations sorted by order
    ob_result = await db.execute(select(Ob).where(Ob.contrato_id == contrato.id).order_by(Ob.orden))
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
    obligaciones_str = (
        "\n".join(f"{i + 1}. [{ob.tipo.value.upper()}] {ob.descripcion}" for i, ob in enumerate(obligaciones))
        or "(no registradas — inferir del objeto del contrato)"
    )

    # Actividades de meses anteriores del mismo contrato — grounding para que el LLM
    # no repita literalmente la redacción de períodos pasados (hasta 20, más recientes).
    actividades_previas_result = await db.execute(
        select(Actividad.descripcion, CuentaCobro.mes, CuentaCobro.anio)
        .join(CuentaCobro, Actividad.cuenta_cobro_id == CuentaCobro.id)
        .where(
            CuentaCobro.contrato_id == contrato.id,
            CuentaCobro.id != cuenta_id,
            Actividad.descripcion.is_not(None),
            Actividad.descripcion != "",
        )
        .order_by(CuentaCobro.anio.desc(), CuentaCobro.mes.desc())
        .limit(20)
    )
    actividades_previas_str = "\n".join(
        f"- {_MESES[mes - 1]} {anio}: {descripcion}" for descripcion, mes, anio in actividades_previas_result.all()
    )

    # Evidencias subidas por el usuario en esta cuenta, con su texto extraído —
    # grounding real para que "Generar todas con IA" no infiera en el vacío.
    # Per-obligación, grounded in CONFIRMED evidencia<->obligación links
    # (justification-context: Per-obligación context consumption); falls back to
    # the legacy flat query only when the cuenta has zero confirmed links.
    evidencias_str = await _contexto_evidencias_por_obligacion(db, cuenta_id, list(obligaciones))

    contexto_usuario = (cuenta.contexto_usuario or "").strip()

    user_content = (
        f"Contrato N° {contrato.numero_contrato}\n"
        f"Entidad: {contrato.entidad or '—'}\n"
        f"Objeto: {contrato.objeto}\n"
        f"Período a facturar: {_MESES[cuenta.mes - 1]} {cuenta.anio}\n\n"
        f"OBLIGACIONES CONTRACTUALES:\n{obligaciones_str}\n"
        + (
            f"\nRESUMEN DEL USUARIO (lo que dice haber hecho este mes — úsalo como guía "
            f"principal de QUÉ se hizo):\n{contexto_usuario}\n"
            if contexto_usuario
            else ""
        )
        + (
            f"\nEVIDENCIAS SUBIDAS (contenido extraído de los archivos del período — "
            f"apóyate en ellas para concretar las actividades):\n{evidencias_str}\n"
            if evidencias_str
            else ""
        )
        + (
            f"\nACTIVIDADES DE MESES ANTERIORES (NO repitas su redacción literal):\n{actividades_previas_str}\n"
            if actividades_previas_str
            else ""
        )
    )
    if texto_contrato:
        user_content += f"\nTEXTO DEL CONTRATO (fragmento):\n{texto_contrato[:3000]}"

    if periodo_inicio is not None and periodo_fin is not None:
        user_content += (
            f"\nVENTANA DE FECHAS ACOTADA (justifica solo dentro de este rango): "
            f"{periodo_inicio.isoformat()} a {periodo_fin.isoformat()}\n"
        )

    # Mismo modelo de calidad que evidence_justify/cruzar: el default de sesión
    # (Groq 8B) rompe el formato y produce redacción pobre.
    llm = get_llm(model=settings.LLM_EXTRACTION_MODEL or None)
    messages = [
        LLMMessage(role="system", content=ACTIVIDADES_GENERATION_PROMPT),
        LLMMessage(role="user", content=user_content),
    ]

    try:
        resp = await llm.complete(messages, temperature=0.3, max_tokens=8192)
    except Exception as exc:
        await logger.aerror("generar_actividades_error", cuenta_id=str(cuenta_id), error=str(exc))
        raise ValidationError(f"Error al generar actividades con el agente: {exc}") from exc

    actividades_data = _parse_actividades_llm(resp.content, list(obligaciones))

    if not actividades_data:
        raise ValidationError(
            "El agente no pudo generar actividades con el formato esperado. "
            "Use POST /actividades/desde-texto para ingresar las actividades manualmente."
        )

    # Regenerar REEMPLAZA: cualquier Actividad existente de la misma obligación en
    # esta cuenta (stub de evidencias, generación anterior, o descubrimiento) se
    # reescribe en su lugar en vez de insertar una fila duplicada. Mantener la fila
    # conserva los links Evidencia.actividad_id que ya cuelgan de ella. Con varias
    # filas por obligación se reescribe la más reciente (las demás quedan para
    # borrado manual — eliminarlas arrastraría sus evidencias).
    stubs_result = await db.execute(
        select(Actividad).where(Actividad.cuenta_cobro_id == cuenta_id).order_by(Actividad.created_at)
    )
    stubs_con_evidencia: dict[uuid.UUID | None, Actividad] = {act.obligacion_id: act for act in stubs_result.scalars()}

    created: list[ActividadResponse] = []
    for data in actividades_data:
        stub = stubs_con_evidencia.pop(data.obligacion_id, None)
        if stub is not None:
            stub.descripcion = data.descripcion
            stub.justificacion = data.justificacion
            stub.justificacion_origen = JustificacionOrigen.LLM
            stub.fecha_realizacion = data.fecha_realizacion
            await db.flush()
            await db.refresh(stub)
            created.append(ActividadResponse.model_validate(stub))
            continue
        act = Actividad(
            cuenta_cobro_id=cuenta_id,
            obligacion_id=data.obligacion_id,
            descripcion=data.descripcion,
            justificacion=data.justificacion,
            justificacion_origen=JustificacionOrigen.LLM,
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


async def crear_actividades_desde_obligaciones(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> ActividadesBulkResponse:
    """Seed one activity per contract obligation (deterministic, NO LLM).

    Bridges the obligaciones→actividades gap without any model call: each obligation
    becomes a baseline activity (descripcion = the obligation text, linked by
    obligacion_id) that the user then edits. Lets the informe be generated when only
    obligaciones were loaded.
    """
    from app.models.obligacion import Obligacion as Ob

    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"No se pueden crear actividades en estado '{cuenta.estado}'. Solo se permite en borrador o rechazada."
        )

    ob_result = await db.execute(select(Ob).where(Ob.contrato_id == cuenta.contrato_id).order_by(Ob.orden))
    obligaciones = list(ob_result.scalars().all())
    if not obligaciones:
        raise ValidationError(
            "El contrato no tiene obligaciones registradas. Regístrelas en "
            "POST /contratos/{id}/obligaciones o ingrese actividades manualmente."
        )

    created: list[ActividadResponse] = []
    for ob in obligaciones:
        act = Actividad(
            cuenta_cobro_id=cuenta_id,
            obligacion_id=ob.id,
            descripcion=ob.descripcion,
            justificacion="",
            fecha_realizacion=None,
        )
        db.add(act)
        await db.flush()
        await db.refresh(act)
        created.append(ActividadResponse.model_validate(act))

    await logger.ainfo(
        "actividades_desde_obligaciones",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        cantidad=len(created),
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
    elif nuevo_estado == EstadoCuentaCobro.BORRADOR:
        # Reopening (e.g. ENVIADA -> BORRADOR) clears the radicación stamp so a
        # fresh radicar_cuenta call re-stamps it, AND the stale PDF reference
        # (REL-002): otherwise obtener_url_pdf and the email/Drive senders could
        # keep serving the pre-edit PDF after the user edits and re-radica. The
        # storage blob itself is left in place (no delete); only the pointer is
        # cleared, so generar_pdf must be called again before it's reachable.
        cuenta.fecha_envio = None
        cuenta.pdf_storage_key = None

    await db.flush()

    await logger.ainfo(
        "cuenta_cobro_estado_cambiado",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        estado_anterior=estado_actual,
        estado_nuevo=nuevo_estado,
    )
    return await _reload_cuenta_response(db, cuenta_id)


async def radicar_cuenta(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
) -> CuentaCobroResponse:
    """Submit (radicar) a CuentaCobro, gating the transition on coherence + checklist.

    Only allowed from BORRADOR or RECHAZADA. When `COHERENCE_GATE_ENABLED` (default
    True), first runs `coherence_validator_service.validar_coherencia` and blocks
    with `ValidationError(code=COHERENCE_CHECK_FAILED)` on any HARD finding — SOFT
    findings are logged as a warning and never block. Then builds/refreshes the
    document checklist via `checklist_service.construir_checklist_completo` and
    blocks the submission when mandatory requisitos are still pending, surfacing
    their labels. The actual state transition (and `fecha_envio` stamping) is
    delegated to `cambiar_estado`, which owns the state machine — this function
    never mutates `cuenta.estado` directly.
    """
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"No se puede radicar una cuenta en estado '{cuenta.estado}'. Solo se permite en borrador o rechazada."
        )

    advertencias: list[FindingOut] = []
    if settings.COHERENCE_GATE_ENABLED:
        findings = await coherence_validator_service.validar_coherencia(db, usuario_id, cuenta_id)
        hallazgos_duros = [f for f in findings if f.severity == Severity.HARD]
        if hallazgos_duros:
            detalles = "; ".join(f"[{f.rule_id}] {f.mensaje}" for f in hallazgos_duros)
            raise ValidationError(
                f"No se puede radicar: se encontraron inconsistencias. {detalles}",
                code=COHERENCE_CHECK_FAILED,
            )
        if findings:
            # Only SOFT findings survive past the HARD-finding raise above.
            advertencias = [
                FindingOut(
                    rule_id=f.rule_id,
                    severity=f.severity,
                    codigo=f.codigo,
                    mensaje=f.mensaje,
                    contexto=f.contexto,
                )
                for f in findings
            ]
            await logger.awarning(
                "radicar_cuenta.coherencia_advertencias",
                cuenta_id=str(cuenta_id),
                usuario_id=str(usuario_id),
                advertencias=len(findings),
            )

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    resumen = payload["resumen"]
    if not resumen["radicacion_lista"]:
        pendientes = ", ".join(resumen["lista_pendientes"]) or "requisitos pendientes por definir"
        raise ValidationError(
            f"No se puede radicar: faltan requisitos del checklist. Pendientes: {pendientes}.",
            code=CHECKLIST_INCOMPLETE,
        )

    resultado = await cambiar_estado(db, usuario_id, cuenta_id, EstadoCuentaCobro.ENVIADA)
    if advertencias:
        # `advertencias_coherencia` (task 3.12c) lets callers see the SOFT findings that
        # let radicación proceed without a second `validar_coherencia_cuenta` call.
        resultado.advertencias_coherencia = advertencias
    return resultado


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


async def _borrar_storage_keys_best_effort(
    storage: StoragePort, keys: Sequence[str], **log_context: str
) -> tuple[int, int]:
    """Best-effort `storage.delete` for each key — never raises, logs and moves on.
    Shared by `eliminar_cuenta_cobro` and `purgar_huerfanos_cuentas`. Returns (ok, failed)."""
    ok = failed = 0
    for key in keys:
        try:
            await storage.delete(key)
            ok += 1
        except Exception as exc:
            failed += 1
            await logger.awarning("cuenta_hard_delete_storage_failed", key=key, error=str(exc), **log_context)
    return ok, failed


def _paquete_prefix(usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> str:
    """Deterministic radicación package storage prefix (mirrors
    `paquete_service._clave_paquete`'s `paquetes/{usuario_id}/{cuenta_id}/` — not
    imported from there to avoid a `cuenta_cobro_service` <-> `paquete_service`
    import cycle, since `paquete_service` already imports this module)."""
    return f"paquetes/{usuario_id}/{cuenta_id}/"


async def _listar_y_borrar_prefix_best_effort(storage: StoragePort, prefix: str, **log_context: str) -> tuple[int, int]:
    """List every object under `prefix` and best-effort delete each. Shared by
    `eliminar_cuenta_cobro` and `purgar_huerfanos_cuentas`'s tombstone loop. A
    failing `list_objects` counts as one failure and yields zero further keys —
    it never raises out, same contract as `_borrar_storage_keys_best_effort`."""
    try:
        objetos = await storage.list_objects(prefix)
    except Exception as exc:
        await logger.awarning("cuenta_hard_delete_storage_failed", key=prefix, error=str(exc), **log_context)
        return 0, 1
    return await _borrar_storage_keys_best_effort(storage, [o.key for o in objetos], **log_context)


async def eliminar_cuenta_cobro(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    storage: StoragePort,
) -> None:
    """Hard-delete a CuentaCobro and every row scoped to it. Allowed for borrador,
    rechazada, and enviada; blocked once aprobada or pagada (those represent a
    settled/paid financial record).

    Soft-delete used to leave a `deleted_at`-marked tombstone row behind. The
    `uq_contrato_mes_anio` unique index has no `WHERE deleted_at IS NULL`
    predicate, so that tombstone kept blocking recreation of a cuota for the
    same contrato/mes/anio — the actual conflict this hard-delete fixes.

    Deletes are explicit and ordered (not just DB `ondelete` cascades): SQLite
    (tests, and possibly dev) does not enforce FK constraints, so relying on
    cascade alone would silently leave orphaned rows there even though
    Postgres (prod) would enforce them. Everything needed after the delete
    (storage keys) is captured into plain variables BEFORE the commit — the
    session expires all ORM attributes on commit, so the `cuenta` object
    itself must not be touched afterward.
    """
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado in (EstadoCuentaCobro.APROBADA, EstadoCuentaCobro.PAGADA):
        raise ValidationError("No se puede eliminar una cuenta aprobada o pagada.")

    pdf_storage_key = cuenta.pdf_storage_key

    actividad_ids = [
        row[0] for row in (await db.execute(select(Actividad.id).where(Actividad.cuenta_cobro_id == cuenta_id))).all()
    ]

    evidencia_rows = (
        (
            await db.execute(
                select(Evidencia.id, Evidencia.storage_key).where(Evidencia.actividad_id.in_(actividad_ids))
            )
        ).all()
        if actividad_ids
        else []
    )
    evidencia_ids = [row[0] for row in evidencia_rows]
    evidencia_storage_keys = [row[1] for row in evidencia_rows if row[1]]

    if evidencia_ids:
        await db.execute(sa_delete(EvidenciaObligacion).where(EvidenciaObligacion.evidencia_id.in_(evidencia_ids)))
        await db.execute(sa_delete(Evidencia).where(Evidencia.id.in_(evidencia_ids)))

    await db.execute(
        sa_delete(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id == cuenta_id)
    )

    if actividad_ids:
        await db.execute(sa_delete(Actividad).where(Actividad.id.in_(actividad_ids)))

    await db.execute(sa_delete(BorradorCuentaCobro).where(BorradorCuentaCobro.cuenta_cobro_id == cuenta_id))

    documento_ids = [
        row[0]
        for row in (
            await db.execute(select(DocumentoCuentaCobro.id).where(DocumentoCuentaCobro.cuenta_cobro_id == cuenta_id))
        ).all()
    ]
    if documento_ids:
        await db.execute(
            sa_delete(DocumentoRequisitoVinculo).where(
                DocumentoRequisitoVinculo.documento_cuenta_cobro_id.in_(documento_ids)
            )
        )
        await db.execute(sa_delete(DocumentoCuentaCobro).where(DocumentoCuentaCobro.id.in_(documento_ids)))

    await db.execute(
        sa_delete(DocumentoChecklistCandidato).where(DocumentoChecklistCandidato.cuenta_cobro_id == cuenta_id)
    )

    await db.execute(sa_delete(RequisitoCuenta).where(RequisitoCuenta.cuenta_cobro_id == cuenta_id))

    # Not owned by the cuenta (contract-level / cross-cuenta records) — only the
    # nullable pointer is cleared, the row itself survives.
    await db.execute(
        sa_update(DocumentoFuente).where(DocumentoFuente.cuenta_cobro_id == cuenta_id).values(cuenta_cobro_id=None)
    )
    await db.execute(
        sa_update(Conversacion).where(Conversacion.cuenta_cobro_id == cuenta_id).values(cuenta_cobro_id=None)
    )

    await db.execute(sa_delete(CuentaCobro).where(CuentaCobro.id == cuenta_id))
    await db.commit()

    await logger.ainfo(
        "cuenta_cobro_eliminada_hard",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        actividades=len(actividad_ids),
        evidencias=len(evidencia_ids),
    )

    # Storage cleanup is BEST-EFFORT and runs AFTER the commit above — the DB
    # deletion is the source of truth and must never be rolled back or failed
    # by a storage-side error.
    keys_to_delete = [*evidencia_storage_keys, *([pdf_storage_key] if pdf_storage_key else [])]
    await _borrar_storage_keys_best_effort(storage, keys_to_delete, cuenta_id=str(cuenta_id))
    await _listar_y_borrar_prefix_best_effort(storage, _paquete_prefix(usuario_id, cuenta_id), cuenta_id=str(cuenta_id))


def _huerfanos_subqueries() -> tuple[Any, Any, Any, Any]:
    """Build the four validity subqueries fresh — NEVER materialize these into
    Python sets. Each is embedded directly into every DELETE/UPDATE's WHERE
    clause below, so the DB evaluates it fresh at THAT statement's own
    execution instant (RISK-001): a cuenta committed by a live writer between
    two of the ~10 statements in `purgar_huerfanos_cuentas` is already visible
    to every subquery run after it commits — a frozen Python set captured once
    at function start could never see it, and would purge its children as
    false orphans.
    """
    from app.models.obligacion import Obligacion

    valid_cuentas = select(CuentaCobro.id).where(CuentaCobro.deleted_at.is_(None))
    good_actividades = select(Actividad.id).where(Actividad.cuenta_cobro_id.in_(valid_cuentas))
    good_evidencias = select(Evidencia.id).where(Evidencia.actividad_id.in_(good_actividades))
    valid_obligaciones = select(Obligacion.id)
    return valid_cuentas, good_actividades, good_evidencias, valid_obligaciones


async def _contar_huerfanos(
    db: AsyncSession, valid_cuentas: Any, good_actividades: Any, good_evidencias: Any, valid_obligaciones: Any
) -> dict[str, int]:
    """Dry-run counterpart of `purgar_huerfanos_cuentas` — the exact same
    conditions, COUNT instead of DELETE/UPDATE, zero mutations, zero storage
    calls."""

    async def _count(stmt: Any) -> int:
        return (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()

    documento_huerfano_ids = select(DocumentoCuentaCobro.id).where(
        DocumentoCuentaCobro.cuenta_cobro_id.not_in(valid_cuentas)
    )

    counts = {
        "cuenta_cobro": await _count(select(CuentaCobro.id).where(CuentaCobro.deleted_at.is_not(None))),
        "actividad": await _count(select(Actividad.id).where(Actividad.cuenta_cobro_id.not_in(valid_cuentas))),
        "evidencia": await _count(select(Evidencia.id).where(Evidencia.actividad_id.not_in(good_actividades))),
        "evidencia_obligacion": await _count(
            select(EvidenciaObligacion.id).where(
                or_(
                    EvidenciaObligacion.evidencia_id.not_in(good_evidencias),
                    (EvidenciaObligacion.obligacion_id.is_not(None))
                    & (EvidenciaObligacion.obligacion_id.not_in(valid_obligaciones)),
                )
            )
        ),
        "clasificacion_job": await _count(
            select(ClasificacionEvidenciasJob.id).where(
                ClasificacionEvidenciasJob.cuenta_cobro_id.not_in(valid_cuentas)
            )
        ),
        "borrador": await _count(
            select(BorradorCuentaCobro.id).where(BorradorCuentaCobro.cuenta_cobro_id.not_in(valid_cuentas))
        ),
        "documento_cuenta_cobro": await _count(documento_huerfano_ids),
        "documento_requisito_vinculo": await _count(
            select(DocumentoRequisitoVinculo.id).where(
                DocumentoRequisitoVinculo.documento_cuenta_cobro_id.in_(documento_huerfano_ids)
            )
        ),
        "documento_checklist_candidato": await _count(
            select(DocumentoChecklistCandidato.id).where(
                DocumentoChecklistCandidato.cuenta_cobro_id.not_in(valid_cuentas)
            )
        ),
        "requisito_cuenta": await _count(
            select(RequisitoCuenta.id).where(RequisitoCuenta.cuenta_cobro_id.not_in(valid_cuentas))
        ),
        "documento_fuente_actualizados": await _count(
            select(DocumentoFuente.id).where(
                DocumentoFuente.cuenta_cobro_id.is_not(None), DocumentoFuente.cuenta_cobro_id.not_in(valid_cuentas)
            )
        ),
        "conversacion_actualizadas": await _count(
            select(Conversacion.id).where(
                Conversacion.cuenta_cobro_id.is_not(None), Conversacion.cuenta_cobro_id.not_in(valid_cuentas)
            )
        ),
        "storage_keys_ok": 0,
        "storage_keys_failed": 0,
    }
    await logger.ainfo("purga_huerfanos_dry_run", **counts)
    return counts


async def purgar_huerfanos_cuentas(db: AsyncSession, storage: StoragePort, *, dry_run: bool = False) -> dict[str, int]:
    """Set-based cleanup of legacy soft-delete leftovers: tombstoned `CuentaCobro`
    rows (`deleted_at IS NOT NULL`) whose children were never removed, PLUS any
    row anywhere whose FK chain is broken (points at a cuenta that is tombstoned
    or altogether gone). Mirrors `eliminar_cuenta_cobro`'s FK-safe explicit
    order, applied across every orphan at once instead of one cuenta at a time.

    A row is "orphan" here if its (possibly indirect) `cuenta_cobro_id` is NOT
    in the healthy set — this single condition covers both "points at a
    tombstone that still physically exists" and "points at nothing at all"
    (a truly dangling FK), since neither case can appear in the healthy set.
    Safe to re-run: a clean DB purges zero rows.

    `dry_run=True` reports the exact same candidate counts via `_contar_huerfanos`
    (COUNT instead of DELETE/UPDATE) and returns before any mutation or storage
    call — used by `scripts/cleanup_orphans.py --dry-run`.
    """
    valid_cuentas, good_actividades, good_evidencias, valid_obligaciones = _huerfanos_subqueries()

    if dry_run:
        return await _contar_huerfanos(db, valid_cuentas, good_actividades, good_evidencias, valid_obligaciones)

    # --- Evidencia (collect storage_key first; the concrete ids collected here
    # are safe to reuse for the DELETE right below — they're specific rows
    # already decided, not a re-evaluated validity filter). ---
    evidencia_rows = (
        await db.execute(
            select(Evidencia.id, Evidencia.storage_key).where(Evidencia.actividad_id.not_in(good_actividades))
        )
    ).all()
    orphan_evidencia_ids = [row[0] for row in evidencia_rows]
    orphan_evidencia_storage_keys = [row[1] for row in evidencia_rows if row[1]]

    # --- EvidenciaObligacion: dangling evidencia_id, dangling/invalid obligacion_id,
    # or evidencia belongs to an actividad of an invalid cuenta (covered by
    # `evidencia_id NOT IN good_evidencias`, since that subquery already
    # excludes those). ---
    enlace_result = await db.execute(
        sa_delete(EvidenciaObligacion).where(
            or_(
                EvidenciaObligacion.evidencia_id.not_in(good_evidencias),
                (EvidenciaObligacion.obligacion_id.is_not(None))
                & (EvidenciaObligacion.obligacion_id.not_in(valid_obligaciones)),
            )
        )
    )
    count_evidencia_obligacion = enlace_result.rowcount or 0

    if orphan_evidencia_ids:
        await db.execute(sa_delete(Evidencia).where(Evidencia.id.in_(orphan_evidencia_ids)))

    job_result = await db.execute(
        sa_delete(ClasificacionEvidenciasJob).where(ClasificacionEvidenciasJob.cuenta_cobro_id.not_in(valid_cuentas))
    )

    actividad_result = await db.execute(sa_delete(Actividad).where(Actividad.cuenta_cobro_id.not_in(valid_cuentas)))

    borrador_result = await db.execute(
        sa_delete(BorradorCuentaCobro).where(BorradorCuentaCobro.cuenta_cobro_id.not_in(valid_cuentas))
    )

    documento_huerfano_ids = select(DocumentoCuentaCobro.id).where(
        DocumentoCuentaCobro.cuenta_cobro_id.not_in(valid_cuentas)
    )
    vinculo_result = await db.execute(
        sa_delete(DocumentoRequisitoVinculo).where(
            DocumentoRequisitoVinculo.documento_cuenta_cobro_id.in_(documento_huerfano_ids)
        )
    )
    documento_result = await db.execute(
        sa_delete(DocumentoCuentaCobro).where(DocumentoCuentaCobro.id.in_(documento_huerfano_ids))
    )

    candidato_result = await db.execute(
        sa_delete(DocumentoChecklistCandidato).where(DocumentoChecklistCandidato.cuenta_cobro_id.not_in(valid_cuentas))
    )

    requisito_result = await db.execute(
        sa_delete(RequisitoCuenta).where(RequisitoCuenta.cuenta_cobro_id.not_in(valid_cuentas))
    )

    # Not owned by any cuenta (contract-level / cross-cuenta records) — only the
    # nullable pointer is cleared, the rows themselves survive. Explicit
    # `.is_not(None)` guard (RELIABILITY-002): `col.not_in(subquery)` where the
    # subquery returns ZERO rows (e.g. zero valid cuentas system-wide) evaluates
    # to TRUE for every value of `col` under standard SQL semantics — INCLUDING
    # NULL — so without this guard a never-linked row would be wrongly touched.
    fuente_result = await db.execute(
        sa_update(DocumentoFuente)
        .where(DocumentoFuente.cuenta_cobro_id.is_not(None), DocumentoFuente.cuenta_cobro_id.not_in(valid_cuentas))
        .values(cuenta_cobro_id=None)
    )
    conversacion_result = await db.execute(
        sa_update(Conversacion)
        .where(Conversacion.cuenta_cobro_id.is_not(None), Conversacion.cuenta_cobro_id.not_in(valid_cuentas))
        .values(cuenta_cobro_id=None)
    )

    # --- Tombstones themselves — collect pdf_storage_key + the usuario_id needed
    # for the deterministic paquete prefix BEFORE deleting the rows. Tombstones
    # are already-soft-deleted rows: no live writer races to guard against here,
    # so a materialized list is fine (unlike the validity subqueries above).
    tombstones = (
        await db.execute(
            select(CuentaCobro.id, CuentaCobro.pdf_storage_key, Contrato.usuario_id)
            .outerjoin(Contrato, Contrato.id == CuentaCobro.contrato_id)
            .where(CuentaCobro.deleted_at.is_not(None))
        )
    ).all()
    cuenta_result = await db.execute(sa_delete(CuentaCobro).where(CuentaCobro.deleted_at.is_not(None)))

    await db.commit()

    counts = {
        "cuenta_cobro": cuenta_result.rowcount or 0,
        "actividad": actividad_result.rowcount or 0,
        "evidencia": len(orphan_evidencia_ids),
        "evidencia_obligacion": count_evidencia_obligacion,
        "clasificacion_job": job_result.rowcount or 0,
        "borrador": borrador_result.rowcount or 0,
        "documento_cuenta_cobro": documento_result.rowcount or 0,
        "documento_requisito_vinculo": vinculo_result.rowcount or 0,
        "documento_checklist_candidato": candidato_result.rowcount or 0,
        "requisito_cuenta": requisito_result.rowcount or 0,
        "documento_fuente_actualizados": fuente_result.rowcount or 0,
        "conversacion_actualizadas": conversacion_result.rowcount or 0,
    }

    await logger.ainfo("purga_huerfanos_done", **counts)

    # Storage cleanup is BEST-EFFORT and runs AFTER the commit above, exactly
    # like `eliminar_cuenta_cobro`.
    ok, failed = await _borrar_storage_keys_best_effort(
        storage, orphan_evidencia_storage_keys, origen="purga_huerfanos"
    )

    pdf_keys = [row[1] for row in tombstones if row[1]]
    ok2, failed2 = await _borrar_storage_keys_best_effort(storage, pdf_keys, origen="purga_huerfanos")
    ok += ok2
    failed += failed2

    for tombstone_id, _pdf_key, usuario_id in tombstones:
        if usuario_id is None:
            continue  # contrato gone too — no reliable prefix to list
        obj_ok, obj_failed = await _listar_y_borrar_prefix_best_effort(
            storage, _paquete_prefix(usuario_id, tombstone_id), origen="purga_huerfanos"
        )
        ok += obj_ok
        failed += obj_failed

    counts["storage_keys_ok"] = ok
    counts["storage_keys_failed"] = failed
    return counts


async def actualizar_cuenta_cobro(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    cuenta_id: uuid.UUID,
    data: CuentaCobroUpdate,
) -> CuentaCobroResponse:
    """Partially update a CuentaCobro (mes/anio/valor). Only allowed in BORRADOR or RECHAZADA."""
    cuenta = await _get_cuenta_con_ownership(db, usuario_id, cuenta_id)

    if cuenta.estado not in (EstadoCuentaCobro.BORRADOR, EstadoCuentaCobro.RECHAZADA):
        raise ValidationError(
            f"Solo se pueden editar cuentas en estado 'borrador' o 'rechazada'. Estado actual: '{cuenta.estado}'."
        )

    nuevo_mes = data.mes if data.mes is not None else cuenta.mes
    nuevo_anio = data.anio if data.anio is not None else cuenta.anio

    if nuevo_mes != cuenta.mes or nuevo_anio != cuenta.anio:
        existing = await db.execute(
            select(CuentaCobro).where(
                CuentaCobro.contrato_id == cuenta.contrato_id,
                CuentaCobro.mes == nuevo_mes,
                CuentaCobro.anio == nuevo_anio,
                CuentaCobro.id != cuenta.id,
                CuentaCobro.deleted_at.is_(None),
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise AlreadyExistsError(
                "CuentaCobro", f"contrato={cuenta.contrato_id} mes={nuevo_mes}/{nuevo_anio}", code=CUENTA_MES_DUPLICADA
            )
        cuenta.mes = nuevo_mes
        cuenta.anio = nuevo_anio

    if data.valor is not None:
        cuenta.valor = float(data.valor)

    if data.informe_final is not None and data.informe_final != cuenta.informe_final:
        if data.informe_final:
            await _verificar_conflicto_posicion(db, cuenta.contrato_id, informe_final=True, excluir_cuenta_id=cuenta.id)
        cuenta.informe_final = data.informe_final

    # fecha_transaccion is nullable: null is a meaningful value (clears the date),
    # so we key off model_fields_set (was the field sent?) rather than `is not None`.
    if "fecha_transaccion" in data.model_fields_set:
        cuenta.fecha_transaccion = data.fecha_transaccion

    # contexto_usuario follows the same omitted-vs-explicit-null contract.
    if "contexto_usuario" in data.model_fields_set:
        cuenta.contexto_usuario = data.contexto_usuario.strip() if data.contexto_usuario else None

    # consecutivo_ds follows the same omitted-vs-explicit-null contract (G4).
    if "consecutivo_ds" in data.model_fields_set:
        cuenta.consecutivo_ds = data.consecutivo_ds.strip() if data.consecutivo_ds else None

    await db.flush()
    await logger.ainfo(
        "cuenta_cobro_actualizada",
        cuenta_id=str(cuenta_id),
        usuario_id=str(usuario_id),
        mes=cuenta.mes,
        anio=cuenta.anio,
    )
    return await _reload_cuenta_response(db, cuenta.id)
