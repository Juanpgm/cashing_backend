"""Contrato service — CRUD and obligaciones management."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.exceptions import NotFoundError, ValidationError
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro, EstadoCuentaCobro
from app.models.documento_fuente import DocumentoFuente, TipoDocumentoFuente
from app.models.obligacion import Obligacion
from app.models.usuario import Usuario
from app.schemas.contrato import (
    ContratoContextoAgenteResponse,
    ContratoCreate,
    ContratoListItem,
    ContratoResponse,
    ContratoUpdate,
    ObligacionCreate,
    ObligacionResponse,
    PeriodoPendienteResponse,
)

logger = structlog.get_logger("service.contrato")

_ESTADOS_ACTIVOS = {
    EstadoCuentaCobro.ENVIADA,
    EstadoCuentaCobro.APROBADA,
    EstadoCuentaCobro.PAGADA,
}


async def _get_contrato_con_ownership(
    db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID
) -> Contrato:
    result = await db.execute(
        select(Contrato)
        .options(selectinload(Contrato.obligaciones))
        .where(
            Contrato.id == contrato_id,
            Contrato.usuario_id == usuario_id,
            Contrato.deleted_at.is_(None),
        )
    )
    contrato = result.scalar_one_or_none()
    if contrato is None:
        raise NotFoundError("Contrato", str(contrato_id))
    return contrato


async def _reload_contrato_response(db: AsyncSession, contrato_id: uuid.UUID) -> ContratoResponse:
    result = await db.execute(
        select(Contrato)
        .options(selectinload(Contrato.obligaciones))
        .where(Contrato.id == contrato_id)
    )
    return ContratoResponse.model_validate(result.scalar_one())


async def crear_contrato(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    data: ContratoCreate,
) -> ContratoResponse:
    """Create a contract with optional obligaciones."""
    if data.fecha_fin <= data.fecha_inicio:
        raise ValidationError("La fecha de fin debe ser posterior a la fecha de inicio.")

    contrato = Contrato(
        usuario_id=usuario_id,
        numero_contrato=data.numero_contrato,
        objeto=data.objeto,
        valor_total=float(data.valor_total),
        valor_mensual=float(data.valor_mensual),
        fecha_inicio=data.fecha_inicio,
        fecha_fin=data.fecha_fin,
        supervisor_nombre=data.supervisor_nombre,
        entidad=data.entidad,
        dependencia=data.dependencia,
    )
    db.add(contrato)
    await db.flush()

    for ob_data in data.obligaciones:
        db.add(
            Obligacion(
                contrato_id=contrato.id,
                descripcion=ob_data.descripcion,
                tipo=ob_data.tipo,
                orden=ob_data.orden,
            )
        )
    await db.flush()

    await logger.ainfo("contrato_creado", contrato_id=str(contrato.id), usuario_id=str(usuario_id))
    return await _reload_contrato_response(db, contrato.id)


async def listar_contratos(db: AsyncSession, usuario_id: uuid.UUID) -> list[ContratoListItem]:
    """List all active contracts for a user, newest first."""
    result = await db.execute(
        select(Contrato)
        .where(Contrato.usuario_id == usuario_id, Contrato.deleted_at.is_(None))
        .order_by(Contrato.created_at.desc())
    )
    return [ContratoListItem.model_validate(c) for c in result.scalars().all()]


async def obtener_contrato(
    db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID
) -> ContratoResponse:
    """Get a contract with its obligaciones."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)
    return await _reload_contrato_response(db, contrato_id)


async def actualizar_contrato(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    data: ContratoUpdate,
) -> ContratoResponse:
    """Partial update of a contract."""
    contrato = await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    updates = data.model_dump(exclude_unset=True)

    fecha_inicio = updates.get("fecha_inicio", contrato.fecha_inicio)
    fecha_fin = updates.get("fecha_fin", contrato.fecha_fin)
    if fecha_fin <= fecha_inicio:
        raise ValidationError("La fecha de fin debe ser posterior a la fecha de inicio.")

    for field, value in updates.items():
        if field in ("valor_total", "valor_mensual") and value is not None:
            value = float(value)
        setattr(contrato, field, value)

    await db.flush()
    return await _reload_contrato_response(db, contrato_id)


async def eliminar_contrato(
    db: AsyncSession, usuario_id: uuid.UUID, contrato_id: uuid.UUID
) -> None:
    """Soft-delete a contract. Blocked if it has active (enviada/aprobada/pagada) cuentas."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    # Block deletion if any cuenta is in an active state
    result = await db.execute(
        select(CuentaCobro).where(
            CuentaCobro.contrato_id == contrato_id,
            CuentaCobro.estado.in_([e.value for e in _ESTADOS_ACTIVOS]),
            CuentaCobro.deleted_at.is_(None),
        )
    )
    if result.scalar_one_or_none() is not None:
        raise ValidationError(
            "No se puede eliminar el contrato: tiene cuentas de cobro en estado enviada, aprobada o pagada."
        )

    result2 = await db.execute(select(Contrato).where(Contrato.id == contrato_id))
    contrato = result2.scalar_one()
    contrato.deleted_at = datetime.now(UTC)
    await db.flush()
    await logger.ainfo("contrato_eliminado", contrato_id=str(contrato_id), usuario_id=str(usuario_id))


async def agregar_obligacion(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    data: ObligacionCreate,
) -> ObligacionResponse:
    """Add an obligation to a contract."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    ob = Obligacion(
        contrato_id=contrato_id,
        descripcion=data.descripcion,
        tipo=data.tipo,
        orden=data.orden,
    )
    db.add(ob)
    await db.flush()
    await db.refresh(ob)
    return ObligacionResponse.model_validate(ob)


async def eliminar_obligacion(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
    obligacion_id: uuid.UUID,
) -> None:
    """Delete an obligation. Blocked if any activity references it."""
    await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    ob_result = await db.execute(
        select(Obligacion).where(
            Obligacion.id == obligacion_id,
            Obligacion.contrato_id == contrato_id,
        )
    )
    ob = ob_result.scalar_one_or_none()
    if ob is None:
        raise NotFoundError("Obligacion", str(obligacion_id))

    # Block if any actividad references this obligacion
    from app.models.actividad import Actividad

    ref = await db.execute(select(Actividad).where(Actividad.obligacion_id == obligacion_id))
    if ref.scalar_one_or_none() is not None:
        raise ValidationError(
            "No se puede eliminar la obligación: hay actividades de cuentas de cobro que la referencian."
        )

    await db.delete(ob)
    await db.flush()


_MESES_ES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

_SYSTEM_PROMPT_TEMPLATE = """\
Eres un asistente especializado en contratos de prestación de servicios para el Estado colombiano.
Tu función es ayudar al contratista a redactar cuentas de cobro con actividades y justificaciones \
que demuestren el cumplimiento de sus obligaciones contractuales.

## DATOS DEL CONTRATO
- Número: {numero_contrato}
- Entidad: {entidad}
- Dependencia: {dependencia}
- Supervisor: {supervisor}
- Objeto: {objeto}
- Vigencia: {fecha_inicio} al {fecha_fin}
- Valor total: $ {valor_total:,.2f}
- Valor mensual: $ {valor_mensual:,.2f}

## OBLIGACIONES CONTRACTUALES
{obligaciones}

## TEXTO DEL CONTRATO
{texto_contrato}

## INSTRUCCIONES Y DIRECTIVAS DEL USUARIO
{instrucciones}

## REGLAS DE REDACCIÓN
- Redacta actividades en primera persona, pasado, con verbos de acción concretos.
- Cada actividad debe justificarse indicando a qué obligación contractual da cumplimiento.
- Usa lenguaje formal apropiado para documentos oficiales colombianos.
- No inventes datos, fechas ni cifras que no estén en el contexto.
- Asegúrate de que el conjunto de actividades cubra todas las obligaciones del período.
"""


async def listar_periodos_pendientes(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
) -> list[PeriodoPendienteResponse]:
    """Returns all months within the contract's vigencia showing which ones haven't been billed."""
    contrato = await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    # Load existing cuentas (anio, mes) pairs
    cuentas_result = await db.execute(
        select(CuentaCobro.mes, CuentaCobro.anio).where(
            CuentaCobro.contrato_id == contrato_id,
            CuentaCobro.deleted_at.is_(None),
        )
    )
    billed = {(r.anio, r.mes) for r in cuentas_result.all()}

    # Generate every month from fecha_inicio to min(fecha_fin, today)
    today = date.today()
    end = min(contrato.fecha_fin, today)
    current = date(contrato.fecha_inicio.year, contrato.fecha_inicio.month, 1)
    end_month = date(end.year, end.month, 1)

    periodos: list[PeriodoPendienteResponse] = []
    while current <= end_month:
        periodos.append(PeriodoPendienteResponse(
            anio=current.year,
            mes=current.month,
            nombre_mes=_MESES_ES[current.month],
            pendiente=(current.year, current.month) not in billed,
        ))
        # Advance one month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)

    return periodos


async def obtener_contexto_agente(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    contrato_id: uuid.UUID,
) -> ContratoContextoAgenteResponse:
    """Return full context needed by the AI agent to generate a cuenta de cobro."""
    contrato = await _get_contrato_con_ownership(db, usuario_id, contrato_id)

    # Load usuario
    user_result = await db.execute(select(Usuario).where(Usuario.id == usuario_id))
    usuario = user_result.scalar_one()

    # Load documentos
    docs_result = await db.execute(
        select(DocumentoFuente).where(
            DocumentoFuente.contrato_id == contrato_id,
            DocumentoFuente.usuario_id == usuario_id,
        )
    )
    docs = docs_result.scalars().all()

    texto_contrato_docs = [d for d in docs if d.tipo == TipoDocumentoFuente.CONTRATO and d.texto_extraido]
    instrucciones_docs = [d for d in docs if d.tipo == TipoDocumentoFuente.INSTRUCCIONES and d.texto_extraido]

    texto_contrato = texto_contrato_docs[0].texto_extraido if texto_contrato_docs else None
    instrucciones = "\n\n".join(d.texto_extraido for d in instrucciones_docs) if instrucciones_docs else None

    # Load cuentas previas
    cuentas_result = await db.execute(
        select(CuentaCobro).where(
            CuentaCobro.contrato_id == contrato_id,
            CuentaCobro.deleted_at.is_(None),
        ).order_by(CuentaCobro.anio.desc(), CuentaCobro.mes.desc())
    )
    cuentas = cuentas_result.scalars().all()
    cuentas_previas = [
        {"mes": c.mes, "anio": c.anio, "estado": c.estado.value, "valor": float(c.valor)}
        for c in cuentas
    ]

    # Determine readiness
    tiene_texto = bool(texto_contrato)
    tiene_instrucciones = bool(instrucciones)
    tiene_obligaciones = len(contrato.obligaciones) > 0
    listo = tiene_texto and tiene_instrucciones and tiene_obligaciones

    faltantes: list[str] = []
    if not tiene_texto:
        faltantes.append("Texto del contrato (POST /documentos/upload?tipo=contrato&contrato_id=...)")
    if not tiene_instrucciones:
        faltantes.append("Instrucciones para el agente (POST /documentos/upload?tipo=instrucciones&contrato_id=...)")
    if not tiene_obligaciones:
        faltantes.append("Obligaciones contractuales (POST /contratos/{id}/obligaciones)")

    # Build system prompt
    system_prompt: str | None = None
    if tiene_obligaciones or tiene_texto:
        obligaciones_str = "\n".join(
            f"{i + 1}. [{ob.tipo.value.upper()}] {ob.descripcion}"
            for i, ob in enumerate(sorted(contrato.obligaciones, key=lambda o: o.orden))
        ) or "(sin obligaciones registradas)"

        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            numero_contrato=contrato.numero_contrato,
            entidad=contrato.entidad or "—",
            dependencia=contrato.dependencia or "—",
            supervisor=contrato.supervisor_nombre or "—",
            objeto=contrato.objeto,
            fecha_inicio=contrato.fecha_inicio.isoformat(),
            fecha_fin=contrato.fecha_fin.isoformat(),
            valor_total=float(contrato.valor_total),
            valor_mensual=float(contrato.valor_mensual),
            obligaciones=obligaciones_str,
            texto_contrato=(texto_contrato[:4000] if texto_contrato else "(no disponible)"),
            instrucciones=(instrucciones[:2000] if instrucciones else "(no cargadas)"),
        )

    return ContratoContextoAgenteResponse(
        contrato_id=contrato_id,
        numero_contrato=contrato.numero_contrato,
        objeto=contrato.objeto,
        entidad=contrato.entidad,
        dependencia=contrato.dependencia,
        supervisor_nombre=contrato.supervisor_nombre,
        fecha_inicio=contrato.fecha_inicio,
        fecha_fin=contrato.fecha_fin,
        valor_total=contrato.valor_total,
        valor_mensual=contrato.valor_mensual,
        documento_proveedor=contrato.documento_proveedor,
        contratista_nombre=usuario.nombre,
        contratista_cedula=getattr(usuario, "cedula", None),
        obligaciones=[ObligacionResponse.model_validate(o) for o in contrato.obligaciones],
        texto_contrato=texto_contrato,
        instrucciones_usuario=instrucciones,
        cuentas_previas=cuentas_previas,
        system_prompt=system_prompt,
        listo=listo,
        faltantes=faltantes,
    )
