"""API v1 router — aggregates all sub-routers."""

from fastapi import APIRouter

from app.api.v1.actividades import router as actividades_router
from app.api.v1.agent_chat import router as agent_chat_router
from app.api.v1.agent_sessions import router as agent_sessions_router
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.checklist import router as checklist_router
from app.api.v1.contratos import router as contratos_router
from app.api.v1.cuentas_cobro import router as cuentas_cobro_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.documentos import router as documentos_router
from app.api.v1.evidencias import router as evidencias_router
from app.api.v1.health import router as health_router
from app.api.v1.integraciones import router as integraciones_router
from app.api.v1.onboarding import router as onboarding_router
from app.api.v1.pagos import creditos_router
from app.api.v1.pagos import router as pagos_router
from app.api.v1.plantillas import router as plantillas_router
from app.api.v1.requisitos_cuenta import router as requisitos_cuenta_router
from app.api.v1.secop import router as secop_router
from app.api.v1.webhooks import router as webhooks_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(auth_router)
api_v1_router.include_router(chat_router)
api_v1_router.include_router(agent_sessions_router)
api_v1_router.include_router(agent_chat_router)
api_v1_router.include_router(documentos_router)
api_v1_router.include_router(contratos_router)
api_v1_router.include_router(cuentas_cobro_router)
api_v1_router.include_router(checklist_router)
api_v1_router.include_router(requisitos_cuenta_router)
api_v1_router.include_router(health_router)
api_v1_router.include_router(integraciones_router)
api_v1_router.include_router(onboarding_router)
api_v1_router.include_router(secop_router)
api_v1_router.include_router(plantillas_router)
api_v1_router.include_router(actividades_router)
api_v1_router.include_router(evidencias_router)
api_v1_router.include_router(pagos_router)
api_v1_router.include_router(creditos_router)
api_v1_router.include_router(webhooks_router)
api_v1_router.include_router(dashboard_router)

# Debug panel — only mounted in non-production environments
from app.core.config import settings  # noqa: E402

if settings.ENVIRONMENT.lower() in ("development", "dev", "local", "test"):
    from app.api.v1.debug import router as debug_router

    api_v1_router.include_router(debug_router)
