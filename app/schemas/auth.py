"""Auth schemas — request/response models for authentication."""

import uuid

from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    nombre: str = Field(min_length=1, max_length=255)
    cedula: str | None = Field(default=None, max_length=20)
    telefono: str | None = Field(default=None, max_length=20)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


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

    model_config = {"from_attributes": True}


class UpdateUserRequest(BaseModel):
    nombre: str | None = Field(default=None, max_length=255)
    cedula: str | None = Field(default=None, max_length=20)
    telefono: str | None = Field(default=None, max_length=20)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class AdminUpdateUserRequest(BaseModel):
    activo: bool | None = None
    rol: str | None = None
    reset_failed_attempts: bool = False
