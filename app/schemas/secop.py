"""Schemas for SECOP public contracting data endpoints."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from pydantic import BaseModel, Field, model_validator

from app.models.categoria_documento import CategoriaDocumento
from app.schemas.contrato import ContratoResponse


def _to_decimal(v: Any) -> Decimal | None:
    """Safely convert a value from datos_raw (str or number) to Decimal."""
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


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

    # Extra fields extracted from datos_raw (not dedicated DB columns)
    objeto_del_contrato: str | None = None
    valor_facturado: Decimal | None = None
    valor_pendiente_de_pago: Decimal | None = None
    valor_amortizado: Decimal | None = None
    valor_pendiente_de_ejecucion: Decimal | None = None
    nombre_supervisor: str | None = None
    urlproceso: str | None = None
    justificacion_modalidad_de: str | None = None
    dias_adicionados: int | None = None

    # Internal: read from ORM but NOT included in JSON response
    datos_raw: dict | None = Field(None, exclude=True)

    @model_validator(mode="after")
    def _populate_from_raw(self) -> "SecopContratoResponse":
        raw = self.datos_raw or {}
        if not raw:
            return self
        if self.objeto_del_contrato is None:
            self.objeto_del_contrato = raw.get("objeto_del_contrato")
        if self.valor_facturado is None:
            self.valor_facturado = _to_decimal(raw.get("valor_facturado"))
        if self.valor_pendiente_de_pago is None:
            self.valor_pendiente_de_pago = _to_decimal(raw.get("valor_pendiente_de_pago"))
        if self.valor_amortizado is None:
            self.valor_amortizado = _to_decimal(raw.get("valor_amortizado"))
        if self.valor_pendiente_de_ejecucion is None:
            self.valor_pendiente_de_ejecucion = _to_decimal(raw.get("valor_pendiente_de_ejecucion"))
        if self.nombre_supervisor is None:
            self.nombre_supervisor = raw.get("nombre_supervisor")
        if self.urlproceso is None:
            url_val = raw.get("urlproceso")
            if isinstance(url_val, dict):
                url_val = url_val.get("url")
            self.urlproceso = str(url_val) if url_val and str(url_val).startswith("http") else None
        if self.justificacion_modalidad_de is None:
            self.justificacion_modalidad_de = raw.get("justificacion_modalidad_de")
        if self.dias_adicionados is None:
            v = raw.get("dias_adicionados")
            self.dias_adicionados = int(v) if v is not None else None
        return self

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
    url_proceso: str | None = None
    tipo_origen: str | None = None  # derived at runtime; not a DB column
    archivo_identificador: str | None = None  # Marketplace ID when no HTTP URL available
    updated_at: datetime
    categoria: CategoriaDocumento = CategoriaDocumento.OTROS
    categoria_confianza: Decimal | None = None
    categoria_override: bool = False

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
    # Ids of archive datasets that failed during the fetch (e.g. Socrata throttling).
    # Non-empty means the result is partial — few documents may be an artifact, not
    # the real count. Defaults to empty for a complete fetch.
    datasets_con_error: list[str] = []


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


class ArchivoInternoItem(BaseModel):
    """Un archivo dentro de un comprimido (zip/rar)."""

    nombre: str
    tamanio_bytes: int | None = None
    es_directorio: bool = False


class ArchivoComprimidoResponse(BaseModel):
    """Lista de archivos internos de un documento comprimido."""

    doc_id: str
    nombre_archivo: str | None
    extension: str | None
    archivos: list[ArchivoInternoItem]
    error: str | None = None


class SecopImportResult(BaseModel):
    """Resultado de importar contratos SECOP a la base de datos del usuario."""

    documento_proveedor: str
    encontrados_en_secop: int
    importados: int
    actualizados: int = 0
    omitidos_duplicados: int
    omitidos_invalidos: int
    contratos: list[ContratoResponse]
