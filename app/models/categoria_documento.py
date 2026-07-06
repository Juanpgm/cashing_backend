"""CategoriaDocumento enum — global document classification axis."""

import enum


class CategoriaDocumento(enum.StrEnum):
    CONTRATO = "contrato"
    REGISTRO_PRESUPUESTAL = "registro_presupuestal"
    ACTA_INICIO = "acta_inicio"
    RUT = "rut"
    CEDULA = "cedula"
    SEGURIDAD_SOCIAL = "seguridad_social"
    EVIDENCIAS = "evidencias"
    OTROS = "otros"
