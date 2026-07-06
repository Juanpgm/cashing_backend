"""Few-shot preference pipeline — loads approved borradores for in-context learning (Phase 7)."""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.borrador_cuenta_cobro import BorradorCuentaCobro
from app.models.contrato import Contrato
from app.models.cuenta_cobro import CuentaCobro

logger = structlog.get_logger("service.fewshot")

# Maximum number of approved drafts to include as few-shot examples
MAX_FEWSHOT_EXAMPLES = 3


async def get_fewshot_examples(
    db: AsyncSession,
    usuario_id: uuid.UUID,
    max_examples: int = MAX_FEWSHOT_EXAMPLES,
) -> list[dict]:
    """Retrieve the most recent approved borrador drafts for a user.

    These are used as few-shot examples when generating new cuentas de cobro,
    ensuring the agent matches the user's preferred tone and structure.

    Returns:
        List of dicts with keys: contenido, feedback_usuario, version, entidad
    """
    result = await db.execute(
        select(BorradorCuentaCobro, Contrato.entidad)
        .join(CuentaCobro, BorradorCuentaCobro.cuenta_cobro_id == CuentaCobro.id)
        .join(Contrato, CuentaCobro.contrato_id == Contrato.id)
        .where(
            Contrato.usuario_id == usuario_id,
            BorradorCuentaCobro.aprobado.is_(True),
        )
        .order_by(BorradorCuentaCobro.created_at.desc())
        .limit(max_examples)
    )
    rows = result.all()

    examples = []
    for borrador, entidad in rows:
        contenido = borrador.contenido or {}
        html = contenido.get("preview_html") or contenido.get("html") or ""
        if html:
            examples.append(
                {
                    "entidad": entidad or "entidad",
                    "version": borrador.version,
                    "html_preview": html[:2000],  # truncate to avoid token bloat
                    "feedback_usuario": borrador.feedback_usuario,
                }
            )

    await logger.ainfo(
        "fewshot_examples_loaded",
        usuario_id=str(usuario_id),
        count=len(examples),
    )
    return examples


def build_fewshot_prompt_section(examples: list[dict]) -> str:
    """Convert few-shot examples into a prompt section for the doc assembly LLM.

    Returns an empty string when there are no examples (first-time users).
    """
    if not examples:
        return ""

    lines = [
        "\n--- EJEMPLOS DE DOCUMENTOS APROBADOS POR EL USUARIO ---",
        "Usa estos ejemplos como referencia de tono, estructura y estilo preferido:",
        "",
    ]
    for i, ex in enumerate(examples, 1):
        lines.append(f"Ejemplo {i} (entidad: {ex['entidad']}, v{ex['version']}):")
        lines.append(ex["html_preview"])
        if ex.get("feedback_usuario"):
            lines.append(f"  ↳ Feedback del usuario: {ex['feedback_usuario']}")
        lines.append("")

    lines.append("--- FIN DE EJEMPLOS ---\n")
    return "\n".join(lines)
