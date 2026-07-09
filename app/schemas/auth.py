"""Auth schemas — request/response models for authentication."""

import re
import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    nombre: str = Field(min_length=1, max_length=255)
    cedula: str | None = Field(default=None, max_length=20)
    telefono: str | None = Field(default=None, max_length=20)
    invite_code: str | None = Field(
        default=None, max_length=64, description="Invite code, required only when the waitlist gate is enabled"
    )


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class GoogleAuthRequest(BaseModel):
    id_token: str = Field(min_length=1, description="Firebase ID token from signInWithPopup")
    invite_code: str | None = Field(
        default=None, max_length=64, description="Invite code, required only for new accounts when the waitlist gate is enabled"
    )


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    nombre: str
    cedula: str | None
    telefono: str | None
    rol: str
    activo: bool
    creditos_disponibles: int
    photo_url: str | None = None
    provider: str = "email"

    model_config = {"from_attributes": True}


class UpdateUserRequest(BaseModel):
    nombre: str | None = Field(default=None, max_length=255)
    cedula: str | None = Field(default=None, max_length=20)
    telefono: str | None = Field(default=None, max_length=20)

    @field_validator("cedula")
    @classmethod
    def validate_cedula_format(cls, v: str | None) -> str | None:
        if v is not None and not re.match(r"^\d{5,15}$", v):
            raise ValueError("La cédula debe contener entre 5 y 15 dígitos numéricos")
        return v
