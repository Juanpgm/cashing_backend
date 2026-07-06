"""Checklist API — required documents per cuenta de cobro."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser
from app.core.database import get_db
from app.core.exceptions import ValidationError
from app.schemas.checklist import (
    ChecklistResponse,
    ChecklistResumen,
    PatchRequisitoBody,
    RequisitoChecklistItem,
)
from app.services import checklist_autogen_service, checklist_service, cuenta_cobro_service

logger = structlog.get_logger("api.checklist")

router = APIRouter(
    prefix="/cuentas-cobro/{cuenta_id}/checklist",
    tags=["checklist"],
)


@router.get("", response_model=ChecklistResponse)
async def obtener_checklist(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChecklistResponse:
    """Devuelve el estado del checklist de documentos para una cuenta de cobro.

    Incluye:
    - estado de cada requisito (pendiente, detectado, cargado, etc.)
    - documento vinculado (subido o SECOP) si existe
    - candidatos SECOP top-N para cada requisito
    - resumen (cuántos cumplidos / pendientes, si está listo para radicar)
    - árbol lógico de evidencias por obligación (A, B, C…)

    Es idempotente: crea las filas faltantes en la primera llamada.

    Si la cuenta aún no resolvió el gate de definición (`requisitos_modo` es NULL),
    NO materializa el checklist: devuelve `requisitos_definidos=false` con `items`
    vacío para que el frontend muestre el paso de definición.
    """
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)

    if cuenta.requisitos_modo is None:
        return ChecklistResponse(
            cuenta_cobro_id=cuenta.id,
            requisitos_definidos=False,
            items=[],
            resumen=ChecklistResumen(
                total=0,
                cumplidos=0,
                pendientes=0,
                lista_pendientes=[],
                radicacion_lista=False,
            ),
            arbol_evidencias=[],
        )

    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()
    return ChecklistResponse(**payload)


@router.post("/refresh-secop", response_model=ChecklistResponse)
async def refrescar_secop(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ChecklistResponse:
    """Re-escanea el caché SECOP del contrato y reasigna candidatos por requisito.

    Útil cuando se han importado nuevos documentos SECOP después de crear la cuenta.
    No sobreescribe documentos cargados manualmente (estado=cargado).
    """
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)
    await checklist_service.asegurar_checklist(db, cuenta)
    await checklist_service.detectar_desde_secop(db, cuenta)
    payload = await checklist_service.construir_checklist_completo(db, cuenta)
    await db.commit()
    return ChecklistResponse(**payload)


@router.post("/auto-vincular-documentos")
async def auto_vincular_documentos(
    cuenta_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Auto-vincula los DocumentoFuente del contrato a los requisitos del checklist.

    Escanea todos los documentos cargados para el contrato. Cuando la categoría
    del documento coincide con un requisito del checklist que está PENDIENTE (y la
    confianza de clasificación es ≥ 0.6, o el usuario sobreescribió la categoría
    manualmente), lo vincula automáticamente (estado=cargado).

    Solo toca filas PENDIENTE — nunca sobreescribe vínculos ya establecidos.
    Retorna el checklist actualizado más `auto_vinculados: int`.
    """
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)
    await checklist_service.asegurar_checklist(db, cuenta)
    vinculados = await checklist_service.auto_vincular_documentos_fuente(db, cuenta)
    # auto_vincular=False: the linking was already done above, no need to run it again.
    payload = await checklist_service.construir_checklist_completo(db, cuenta, auto_vincular=False)
    await db.commit()
    return {**payload, "auto_vinculados": vinculados}


@router.post("/{requisito_codigo}/generar", response_model=RequisitoChecklistItem)
async def generar_requisito(
    cuenta_id: uuid.UUID,
    requisito_codigo: str,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RequisitoChecklistItem:
    """Genera automáticamente el documento de un requisito y lo vincula como `cargado`.

    Solo aplica a requisitos estándar marcados `permite_autogen` (los informes de
    actividades y de supervisión). El documento se produce a partir de los datos ya
    cargados en la cuenta (contrato, obligaciones, actividades) — sin subir archivos.

    Errores: código no autogenerable o cuenta sin actividades → 422; cuenta ajena → 403.
    """
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)
    # The checklist must be defined first (gate resolved): no informe can be generated
    # for a cuenta whose requisitos have not been set up.
    if cuenta.requisitos_modo is None:
        raise ValidationError(
            "Definí primero los requisitos del checklist de esta cuenta de cobro antes de generar documentos."
        )
    # Idempotent: guarantees the DocumentoCuentaCobro row exists before linking.
    await checklist_service.asegurar_checklist(db, cuenta)
    await checklist_autogen_service.generar_y_vincular(db, user.id, cuenta_id, requisito_codigo)

    # Rebuild full payload and return only this requisito's item.
    # auto_vincular=False: the document we just generated is already linked.
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)
    payload = await checklist_service.construir_checklist_completo(db, cuenta, auto_vincular=False)
    await db.commit()
    item = next(
        (i for i in payload["items"] if i["requisito"]["codigo"] == requisito_codigo),
        None,
    )
    if item is None:
        raise ValidationError(f"Requisito {requisito_codigo} not found in checklist.")
    return RequisitoChecklistItem(**item)


@router.patch("/{requisito_codigo}", response_model=RequisitoChecklistItem)
async def actualizar_requisito(
    cuenta_id: uuid.UUID,
    requisito_codigo: str,
    body: PatchRequisitoBody,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RequisitoChecklistItem:
    """Actualiza un requisito del checklist.

    Acciones soportadas (usar **una** por llamada, además de `observaciones`):
    - `documento_fuente_id` → vincula un DocumentoFuente subido; estado=`cargado`
    - `secop_documento_id` → vincula un documento SECOP; estado=`detectado`
    - `desvincular: true` → remueve enlaces; estado=`pendiente`
    - `no_aplica: true` → estado=`no_aplica`
    - `cumplido_manual: true` → estado=`cumplido_manual`
    - `observaciones` → set/update observaciones (puede combinarse)
    """
    # Ownership check via cuenta
    await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)

    actions = [
        body.documento_fuente_id is not None,
        body.secop_documento_id is not None,
        bool(body.desvincular),
        bool(body.no_aplica),
        bool(body.cumplido_manual),
    ]
    if sum(actions) > 1:
        raise ValidationError(
            "Provide at most one state action per call "
            "(documento_fuente_id, secop_documento_id, desvincular, no_aplica, cumplido_manual)."
        )

    if body.documento_fuente_id is not None:
        await checklist_service.vincular_documento_fuente(db, cuenta_id, requisito_codigo, body.documento_fuente_id)
    elif body.secop_documento_id is not None:
        await checklist_service.vincular_secop_documento(db, cuenta_id, requisito_codigo, body.secop_documento_id)
    elif body.desvincular:
        await checklist_service.desvincular(db, cuenta_id, requisito_codigo)
    elif body.no_aplica:
        await checklist_service.marcar_no_aplica(db, cuenta_id, requisito_codigo)
    elif body.cumplido_manual:
        await checklist_service.marcar_cumplido_manual(db, cuenta_id, requisito_codigo)

    if body.observaciones is not None:
        await checklist_service.set_observaciones(db, cuenta_id, requisito_codigo, body.observaciones)

    await db.commit()

    # Rebuild full payload and return only this requisito's item.
    # auto_vincular=False: the user just made an explicit choice; don't override it.
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, user.id, cuenta_id)
    payload = await checklist_service.construir_checklist_completo(db, cuenta, auto_vincular=False)
    await db.commit()
    # Match standard rows by codigo, custom rows by their requisito_cuenta_id UUID.
    item = next(
        (
            i
            for i in payload["items"]
            if i["requisito"]["codigo"] == requisito_codigo
            or str(i["requisito"].get("requisito_cuenta_id")) == requisito_codigo
        ),
        None,
    )
    if item is None:
        raise ValidationError(f"Requisito {requisito_codigo} not found in checklist.")
    return RequisitoChecklistItem(**item)
