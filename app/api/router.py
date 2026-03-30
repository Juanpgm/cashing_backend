"""API v1 router — aggregates all sub-routers."""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.contratos import router as contratos_router
from app.api.v1.cuentas_cobro import router as cuentas_cobro_router
from app.api.v1.documentos import router as documentos_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth_router)
api_v1_router.include_router(chat_router)
api_v1_router.include_router(documentos_router)
api_v1_router.include_router(contratos_router)
api_v1_router.include_router(cuentas_cobro_router)
