"""Package listing + regenerate (radicacion-stepper, work unit B5).

`listar_paquete` is a READ-ONLY preview of the current package state: reads
package metadata from the deterministic storage prefix via `StoragePort.
list_objects` and composes it with `informe_service.obtener_estado_listo_
pendiente`. Zero storage writes, zero packaging calls, zero credit changes.

`regenerar_paquete` is a thin delegate to `radicacion_prep_service.
preparar_radicacion` — the single fail-closed readiness gate (checklist ->
coherence HARD-stop -> packager with secret scan). It NEVER calls the lighter
`informe_service.generar_zip_evidencias` directly, and it re-implements
NONE of the gate logic: "regenerate" just means "re-run preparar_radicacion".
Because the storage key is deterministic (`paquetes/{usuario_id}/{cuenta_id}/
evidencias-{numero_contrato}-{anio}-{mes:02d}.zip`), regenerating overwrites
the same object — exactly one current package per (cuota, contrato period),
design section 6 ("storage-key lifecycle").
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.storage import get_storage as _get_storage
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.schemas.paquete import ObligacionEstadoOut, PaqueteInfoResponse
from app.services import cuenta_cobro_service, informe_service, radicacion_prep_service
from app.services.radicacion_prep_service import RadicacionPrepResultado


def _clave_paquete(
    usuario_id: uuid.UUID, cuenta_id: uuid.UUID, numero_contrato: str, anio: int, mes: int
) -> tuple[str, str, str]:
    """Mirrors `radicacion_prep_service.preparar_radicacion`'s own key
    construction exactly (`{filename}` there comes from `informe_service.
    generar_zip_evidencias`'s deterministic naming). Returns
    ``(prefix, filename, storage_key)``."""
    filename = f"evidencias-{numero_contrato}-{anio}-{mes:02d}.zip"
    prefix = f"paquetes/{usuario_id}/{cuenta_id}/"
    return prefix, filename, f"{prefix}{filename}"


async def listar_paquete(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> PaqueteInfoResponse:
    """Read-only package listing. Never uploads, never packages, never charges
    credits — only a storage-prefix read (`StoragePort.list_objects`) plus the
    same read-only LISTO/PENDIENTE split `generar_zip_evidencias` uses
    internally."""
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    contrato = cuenta.contrato
    # B7 Fix 4: `_get_cuenta_con_ownership` only filters `CuentaCobro.deleted_at`,
    # not the joined `Contrato.deleted_at` — reject soft-deleted contracts here
    # (new call site) rather than in the shared helper.
    if contrato.deleted_at is not None:
        raise NotFoundError("Contrato", str(contrato.id))
    estado = await informe_service.obtener_estado_listo_pendiente(db, usuario_id, cuenta_id)

    prefix, filename, storage_key = _clave_paquete(
        usuario_id, cuenta_id, contrato.numero_contrato, cuenta.anio, cuenta.mes
    )

    storage = _get_storage(settings.S3_BUCKET_EVIDENCIAS)
    objetos = await storage.list_objects(prefix)
    objeto = next((o for o in objetos if o.key == storage_key), None)

    return PaqueteInfoResponse(
        cuenta_cobro_id=cuenta_id,
        existe=objeto is not None,
        storage_key=storage_key,
        filename=filename,
        size_bytes=objeto.size_bytes if objeto is not None else 0,
        listo_para_radicar=estado.listo_para_radicar,
        pendientes=estado.pendientes,
        obligaciones=[
            ObligacionEstadoOut(obligacion_id=o.obligacion_id, descripcion=o.descripcion, listo=o.listo)
            for o in estado.obligaciones
        ],
    )


async def descargar_paquete(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> tuple[bytes, str]:
    """Pure read (P5): serves the bytes of the last persisted package object
    at the deterministic key. NEVER regenerates, NEVER calls `informe_
    service.generar_zip_evidencias` or any LLM-backed step — a repeated call
    with no regenerate in between returns byte-identical content because it
    reads the same storage object every time.

    Raises `NotFoundError` when nothing has been generated yet — the caller
    must hit `POST /paquete/regenerar` first; this function never triggers it
    itself.

    Concurrency: relies on the storage backend's atomic PutObject
    last-writer-wins overwrite (see module docstring) — a download racing a
    regenerate reads either the complete old object or the complete new one,
    never a torn/partial mix.
    """
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    contrato = cuenta.contrato
    # B7 Fix 4 parity: same soft-deleted-contrato rejection as `listar_paquete`
    # and `regenerar_paquete`.
    if contrato.deleted_at is not None:
        raise NotFoundError("Contrato", str(contrato.id))

    prefix, filename, storage_key = _clave_paquete(
        usuario_id, cuenta_id, contrato.numero_contrato, cuenta.anio, cuenta.mes
    )

    storage = _get_storage(settings.S3_BUCKET_EVIDENCIAS)
    objetos = await storage.list_objects(prefix)
    if not any(o.key == storage_key for o in objetos):
        raise NotFoundError("Paquete", storage_key)

    contenido = await storage.download(storage_key)
    return contenido, filename


async def regenerar_paquete(db: AsyncSession, usuario_id: uuid.UUID, cuenta_id: uuid.UUID) -> RadicacionPrepResultado:
    """Thin delegate — no gate re-implementation, no bypass. All fail-closed
    codes (`CHECKLIST_INCOMPLETE`, `COHERENCE_CHECK_FAILED`, `PACKAGE_
    PENDIENTE`, `SECRET_DETECTED_IN_PACKAGE`) propagate unchanged from
    `preparar_radicacion`.

    B7 Fix 4: pre-checks contrato aliveness (see `listar_paquete`'s comment —
    same rationale) before delegating, so a soft-deleted contract's cuota
    cannot be regenerated/packaged."""
    cuenta = await cuenta_cobro_service._get_cuenta_con_ownership(db, usuario_id, cuenta_id)
    if cuenta.contrato.deleted_at is not None:
        raise NotFoundError("Contrato", str(cuenta.contrato_id))
    return await radicacion_prep_service.preparar_radicacion(db, usuario_id, cuenta_id)
