"""Schemas for SECOP public contracting data endpoints."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel

from app.schemas.contrato import ContratoResponse


class SecopContratoResponse(BaseModel):
    id: uuid.UUID
    id_contrato_secop: str
    cedula_contratista: str
    nombre_contratista: str | None
    tipodocproveedor: str | None
    nombre_entidad: str | None
    nit_entidad: str | None
    sector: str | None
    departamento: str | None
    ciudad: str | None
    proceso_de_compra: str | None
    numero_contrato: str | None
    referencia_del_contrato: str | None
    tipo_de_contrato: str | None
    modalidad_de_contratacion: str | None
    descripcion_del_proceso: str | None
    estado_contrato: str | None
    fecha_de_firma: date | None
    fecha_inicio: date | None
    fecha_fin: date | None
    valor_del_contrato: Decimal | None
    valor_pagado: Decimal | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class SecopProcesoResponse(BaseModel):
    id: uuid.UUID
    id_proceso_secop: str
    referencia_del_proceso: str | None
    nombre_del_procedimiento: str | None
    descripcion: str | None
    entidad: str | None
    nit_entidad: str | None
    departamento_entidad: str | None
    ciudad_entidad: str | None
    fase: str | None
    modalidad_de_contratacion: str | None
    precio_base: Decimal | None
    estado_del_procedimiento: str | None
    fecha_de_publicacion: date | None
    adjudicado: str | None
    duracion: str | None
    unidad_de_duracion: str | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class SecopDocumentoResponse(BaseModel):
    id: uuid.UUID
    id_documento_secop: str
    numero_contrato: str | None
    proceso: str | None
    secop_contrato_id: uuid.UUID | None
    secop_proceso_id: uuid.UUID | None
    nombre_archivo: str | None
    extension: str | None
    descripcion: str | None
    fecha_carga: date | None
    entidad: str | None
    nit_entidad: str | None
    url_descarga: str | None
    updated_at: datetime

    model_config = {"from_attributes": True}


class SecopSincronizarDocumentosResult(BaseModel):
    """Resultado de sincronizar documentos de contratos y procesos SECOP."""

    contratos_procesados: int
    procesos_procesados: int
    documentos_encontrados: int
    documentos_guardados: int
    documentos_omitidos_duplicados: int
    confirmar: bool
    documentos: list[SecopDocumentoResponse]


class SecopContratoDetalleResponse(BaseModel):
    """Contrato con su proceso y documentos asociados."""

    contrato: SecopContratoResponse
    proceso: SecopProcesoResponse | None
    documentos: list[SecopDocumentoResponse]


class SecopConsultaCompletaResponse(BaseModel):
    """Resultado de consulta completa por cédula."""

    cedula: str
    total_contratos: int
    contratos: list[SecopContratoDetalleResponse]


class SecopImportResult(BaseModel):
    """Resultado de importar contratos SECOP a la base de datos del usuario."""

    documento_proveedor: str
    encontrados_en_secop: int
    importados: int
    omitidos_duplicados: int
    omitidos_invalidos: int
    contratos: list[ContratoResponse]
