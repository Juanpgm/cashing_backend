"""Schemas for /configuracion — self-service account-level actions."""

from pydantic import BaseModel, field_validator

_CONFIRMACION_ESPERADA = "BORRAR"


class PurgarDatosPruebaRequest(BaseModel):
    confirmacion: str

    @field_validator("confirmacion")
    @classmethod
    def validar_confirmacion(cls, v: str) -> str:
        if v != _CONFIRMACION_ESPERADA:
            raise ValueError(f'Debes escribir "{_CONFIRMACION_ESPERADA}" para confirmar la purga de datos.')
        return v


class PurgarDatosPruebaResponse(BaseModel):
    cuentas_borradas: int
    obligaciones_reiniciadas: int
    contratos_omitidos: int
