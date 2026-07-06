"""Firebase Admin SDK initialization and ID token verification.

The service account is loaded from the FIREBASE_SERVICE_ACCOUNT_JSON env var
(a JSON string — paste the content of the downloaded service account key file).
On GCP, Application Default Credentials are used as a fallback.
"""

from __future__ import annotations

import asyncio
import json

import structlog

logger = structlog.get_logger("core.firebase")

_initialized = False


def _init_app() -> None:
    global _initialized
    if _initialized:
        return

    import firebase_admin
    from firebase_admin import credentials

    from app.core.config import settings

    sa_path = settings.FIREBASE_SERVICE_ACCOUNT_PATH
    sa_json = settings.FIREBASE_SERVICE_ACCOUNT_JSON

    if sa_path:
        # Local dev: load from file path (avoids multiline env var issues)
        cred = credentials.Certificate(sa_path)
        with open(sa_path) as f:
            project_id = json.load(f)["project_id"]
        firebase_admin.initialize_app(cred, {"projectId": project_id})
    elif sa_json:
        # Production: JSON string set in the platform's env var dashboard
        sa_dict = json.loads(sa_json)
        cred = credentials.Certificate(sa_dict)
        firebase_admin.initialize_app(cred, {"projectId": sa_dict["project_id"]})
    else:
        cred = credentials.ApplicationDefault()
        firebase_admin.initialize_app(cred)
    _initialized = True
    logger.info("firebase_admin_initialized")


async def verify_firebase_token(id_token: str) -> dict:
    """Verify a Firebase ID token and return the decoded claims.

    Runs firebase_admin.auth.verify_id_token (blocking) in a thread executor
    to avoid blocking the asyncio event loop.

    Raises ValueError if the token is invalid or expired.
    """
    _init_app()

    from firebase_admin import auth as fb_auth

    loop = asyncio.get_event_loop()
    decoded = await loop.run_in_executor(None, fb_auth.verify_id_token, id_token)
    return decoded  # type: ignore[return-value]
