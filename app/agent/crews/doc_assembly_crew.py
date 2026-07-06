"""DocAssemblyCrew — parallel document generation via CrewAI.

Generates the complete document set (cuenta de cobro + informe de actividades +
anexos) in parallel.  Falls back gracefully when CrewAI is unavailable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

logger = structlog.get_logger("agent.crews.doc_assembly")


# ---------------------------------------------------------------------------
# Document generator workers
# ---------------------------------------------------------------------------


async def _generate_cuenta_cobro(
    contrato: dict[str, Any],
    actividades: list[dict[str, Any]],
    evidencias: list[dict[str, Any]],
    mes: int,
    anio: int,
) -> dict[str, Any]:
    """Generate the cuenta de cobro document content."""
    from app.adapters.llm import get_llm  # noqa: PLC0415
    from app.agent.prompts.doc_assembly import CUENTA_COBRO_USER, DOC_ASSEMBLY_SYSTEM  # noqa: PLC0415
    from app.schemas.agent import LLMMessage  # noqa: PLC0415

    try:
        llm = get_llm(model="gemini/gemini-2.5-flash")
        entidad = contrato.get("entidad") or "Entidad"
        contratista = contrato.get("contratista") or "Contratista"
        numero = contrato.get("numero_contrato") or "Sin número"
        valor = contrato.get("valor_mensual") or "0"

        actividades_text = "\n".join(
            f"{i + 1}. {a.get('descripcion', '')}" for i, a in enumerate(actividades[:20])
        )

        user_prompt = CUENTA_COBRO_USER.format(
            entidad=entidad,
            contratista=contratista,
            numero_contrato=numero,
            valor=valor,
            mes=mes,
            anio=anio,
            actividades=actividades_text,
            evidencias_resumen=f"{len(evidencias)} evidencias disponibles",
        )
        messages = [
            LLMMessage(role="system", content=DOC_ASSEMBLY_SYSTEM),
            LLMMessage(role="user", content=user_prompt),
        ]
        response = await llm.complete(messages, temperature=0.3, max_tokens=2000)
        html_content = response.content if hasattr(response, "content") else str(response)

        return {
            "type": "cuenta_cobro",
            "html": html_content,
            "metadata": {
                "entidad": entidad,
                "mes": mes,
                "anio": anio,
                "contratista": contratista,
                "numero_contrato": numero,
            },
        }
    except Exception as exc:
        await logger.awarning("cuenta_cobro_generation_failed", error=str(exc))
        return {"type": "cuenta_cobro", "html": "", "error": str(exc)}


async def _generate_informe_actividades(
    contrato: dict[str, Any],
    actividades: list[dict[str, Any]],
    obligaciones: list[dict[str, Any]],
    mes: int,
    anio: int,
) -> dict[str, Any]:
    """Generate the informe de actividades document content."""
    from app.adapters.llm import get_llm  # noqa: PLC0415
    from app.agent.prompts.doc_assembly import DOC_ASSEMBLY_SYSTEM, INFORME_ACTIVIDADES_USER  # noqa: PLC0415
    from app.schemas.agent import LLMMessage  # noqa: PLC0415

    try:
        llm = get_llm(model="gemini/gemini-2.5-flash")
        entidad = contrato.get("entidad") or "Entidad"
        contratista = contrato.get("contratista") or "Contratista"

        actividades_text = "\n".join(
            f"{i + 1}. {a.get('descripcion', '')}" for i, a in enumerate(actividades[:20])
        )
        obligaciones_text = "\n".join(
            f"{i + 1}. {o.get('descripcion', '')}" for i, o in enumerate(obligaciones[:15])
        )

        user_prompt = INFORME_ACTIVIDADES_USER.format(
            entidad=entidad,
            contratista=contratista,
            mes=mes,
            anio=anio,
            actividades=actividades_text,
            obligaciones=obligaciones_text,
        )
        messages = [
            LLMMessage(role="system", content=DOC_ASSEMBLY_SYSTEM),
            LLMMessage(role="user", content=user_prompt),
        ]
        response = await llm.complete(messages, temperature=0.3, max_tokens=2000)
        html_content = response.content if hasattr(response, "content") else str(response)

        return {
            "type": "informe_actividades",
            "html": html_content,
            "metadata": {
                "entidad": entidad,
                "mes": mes,
                "anio": anio,
                "contratista": contratista,
            },
        }
    except Exception as exc:
        await logger.awarning("informe_generation_failed", error=str(exc))
        return {"type": "informe_actividades", "html": "", "error": str(exc)}


async def _generate_anexo(
    contrato: dict[str, Any],
    evidencias: list[dict[str, Any]],
    mes: int,
    anio: int,
) -> dict[str, Any]:
    """Generate a simple evidence annexe listing."""
    entidad = contrato.get("entidad") or "Entidad"
    lines = [
        f"<h2>Anexo — Evidencias de cumplimiento — {mes}/{anio}</h2>",
        f"<p>Contrato: {contrato.get('numero_contrato', 'Sin número')} — {entidad}</p>",
        "<ul>",
    ]
    for ev in evidencias[:30]:
        source = ev.get("source") or ev.get("crew_source", "")
        if source == "gmail":
            lines.append(f"<li>Email: {ev.get('subject', 'Sin asunto')} ({ev.get('date', '')})</li>")
        elif source == "drive":
            link = ev.get("webViewLink", "#")
            name = ev.get("name", "Archivo")
            lines.append(f'<li>Drive: <a href="{link}">{name}</a></li>')
        elif source == "calendar":
            lines.append(f"<li>Evento: {ev.get('summary', 'Sin título')} ({ev.get('start', '')})</li>")
        else:
            content_preview = (ev.get("content") or ev.get("snippet") or "")[:100]
            lines.append(f"<li>{source}: {content_preview}</li>")
    lines.append("</ul>")

    return {
        "type": "anexo",
        "html": "\n".join(lines),
        "metadata": {"evidencias_count": len(evidencias), "mes": mes, "anio": anio},
    }


# ---------------------------------------------------------------------------
# Public crew class
# ---------------------------------------------------------------------------


class DocAssemblyCrew:
    """Parallel document assembly for cuenta de cobro, informe, and anexo.

    Usage::

        crew = DocAssemblyCrew(
            contrato=state["contrato_extraido"],
            actividades=state.get("actividades_generadas") or [],
            obligaciones=state.get("obligaciones_extraidas") or [],
            evidencias=state.get("deduplicated_evidence") or [],
            mes=state.get("mes") or 0,
            anio=state.get("anio") or 0,
        )
        drafts = await crew.kickoff_async()
        # drafts is a list of dicts with type, html, metadata keys
    """

    def __init__(
        self,
        contrato: dict[str, Any],
        actividades: list[dict[str, Any]] | None = None,
        obligaciones: list[dict[str, Any]] | None = None,
        evidencias: list[dict[str, Any]] | None = None,
        mes: int = 0,
        anio: int = 0,
    ) -> None:
        self.contrato = contrato
        self.actividades = actividades or []
        self.obligaciones = obligaciones or []
        self.evidencias = evidencias or []
        self.mes = mes
        self.anio = anio

    async def kickoff_async(self) -> list[dict[str, Any]]:
        """Generate all documents in parallel and return list of drafts."""
        await logger.ainfo(
            "doc_assembly_crew_start",
            actividades_count=len(self.actividades),
            evidencias_count=len(self.evidencias),
        )

        cuenta_task = _generate_cuenta_cobro(
            self.contrato, self.actividades, self.evidencias, self.mes, self.anio
        )
        informe_task = _generate_informe_actividades(
            self.contrato, self.actividades, self.obligaciones, self.mes, self.anio
        )
        anexo_task = _generate_anexo(self.contrato, self.evidencias, self.mes, self.anio)

        cuenta, informe, anexo = await asyncio.gather(
            cuenta_task, informe_task, anexo_task, return_exceptions=False
        )

        drafts = [cuenta, informe, anexo]

        await logger.ainfo("doc_assembly_crew_done", drafts_count=len(drafts))
        return drafts

    def kickoff(self) -> list[dict[str, Any]]:
        """Synchronous wrapper around kickoff_async."""
        return asyncio.run(self.kickoff_async())

    def to_dict(self) -> dict[str, Any]:
        """Serialisable representation for state/logs."""
        return {
            "crew": "DocAssemblyCrew",
            "actividades_count": len(self.actividades),
            "evidencias_count": len(self.evidencias),
            "mes": self.mes,
            "anio": self.anio,
        }
