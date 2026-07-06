# ruff: noqa: T201 — CLI de demostración: los print son la salida esperada.
"""Demo local del agente 'explorer' de evidencias (Gmail + Drive + Calendar → justificación).

Hace, sin necesidad de frontend:
  1. OAuth loopback (consola): conecta tu cuenta de Google con un OAuth client tipo "Desktop".
  2. Persiste los tokens (encriptados) para un usuario de prueba en la base de datos.
  3. Corre el pipeline de descubrimiento de evidencias y te imprime, por obligación,
     la JUSTIFICACIÓN y los LINKS a las evidencias encontradas.

Requisitos previos (ver el plan):
  - make up  (Postgres) y migraciones aplicadas.
  - .env (o secrets/.env.local) con: GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
    TOKEN_ENCRYPTION_KEY (Fernet) y GEMINI_API_KEY o GROQ_API_KEY.
  - El email de prueba agregado como "test user" en la pantalla de consentimiento OAuth.

Uso:
    uv run python scripts/evidence_demo.py
"""

from __future__ import annotations

import asyncio
import json

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.security import hash_password
from app.models.usuario import Usuario
from app.schemas.google_workspace import EvidenceDiscoveryRequest
from app.services import evidence_discovery_service as eds
from app.services import google_workspace_service as gws
from google_auth_oauthlib.flow import InstalledAppFlow
from sqlalchemy import select

DEMO_EMAIL = "demo-evidencias@cashin.local"

# Edita estas obligaciones y fechas para tu contrato real:
DEMO_REQUEST = EvidenceDiscoveryRequest(
    obligaciones=[
        {"id": "ob1", "descripcion": "Elaborar y entregar informes mensuales de actividades"},
        {"id": "ob2", "descripcion": "Asistir a reuniones de seguimiento del contrato"},
    ],
    fecha_inicio="2026-01-01",
    fecha_fin="2026-01-31",
    supervisor_email=None,
    entidad=None,
)


def _run_loopback_oauth() -> InstalledAppFlow:
    """Abre el navegador para autorizar y devuelve el flow con credenciales."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise SystemExit("Falta GOOGLE_OAUTH_CLIENT_ID/SECRET en el entorno (.env).")

    client_config = {
        "installed": {
            "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=settings.GOOGLE_OAUTH_SCOPES)
    flow.run_local_server(port=0, prompt="consent", access_type="offline")
    return flow


async def _get_or_create_demo_user(db) -> Usuario:
    result = await db.execute(select(Usuario).where(Usuario.email == DEMO_EMAIL))
    user = result.scalar_one_or_none()
    if user:
        return user
    user = Usuario(
        email=DEMO_EMAIL,
        nombre="Demo Evidencias",
        cedula="0000000000",
        telefono="+570000000000",
        password_hash=hash_password("DemoPass123!"),
        rol="contratista",
        activo=True,
        creditos_disponibles=100,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def main() -> None:
    print("=== 1) Autorizando Google (se abrirá el navegador) ===")
    flow = _run_loopback_oauth()
    creds = flow.credentials
    print(f"   Autorizado. Scopes: {creds.scopes}")

    async with async_session_factory() as db:
        user = await _get_or_create_demo_user(db)
        await gws.store_credentials(
            db,
            user.id,
            access_token=creds.token,
            refresh_token=creds.refresh_token,
            scopes=creds.scopes or settings.GOOGLE_OAUTH_SCOPES,
        )
        print(f"   Tokens guardados para usuario {user.email} ({user.id}).")

        print("\n=== 2) Descubriendo evidencias (Gmail + Drive + Calendar) ===")
        resp = await eds.descubrir_evidencias(db, user.id, DEMO_REQUEST)

    print(f"\n{resp.resumen}\n")
    for ob in resp.obligaciones:
        print(f"── Obligación: {ob.descripcion}")
        print(f"   Justificación: {ob.justificacion}")
        if ob.evidencias:
            print("   Evidencias:")
            for ev in ob.evidencias:
                print(f"     - [{ev.source}] {ev.titulo} ({ev.fecha}) → {ev.link}")
        else:
            print("   (sin evidencias)")
        print()

    print("JSON completo:")
    print(json.dumps(resp.model_dump(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
