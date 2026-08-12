"""Sentinel constants shared between the evidence-justification agent node
(`app.agent.nodes.evidence_justify`) and the informe generators
(`app.services.informe_service`).

A standalone leaf module (zero project imports) so either side can import it
without risking a circular import: `informe_service` already pulls in
`app.agent.prompts.*` (which triggers `app.agent`'s package init, in turn
importing `app.agent.graph` and every agent node, including
`evidence_justify`) — if `evidence_justify` imported the constant straight out
of `informe_service` instead, that import chain could re-enter a
partially-initialized `app.agent` package. Keeping the constants here avoids
the question entirely.
"""

from __future__ import annotations

# Obligación sin ninguna evidencia en el período: sentinel determinístico y verbatim
# (nunca meta-texto del LLM sobre "ausencia de evidencias" — inútil en el informe real).
SENTINEL_SIN_EVIDENCIAS = "No he desarrollado esta obligación a corte del presente informe."

# Third-person variant for the supervisión report's tercera-persona conversion —
# a fixed deterministic string, never run through the LLM rewrite (which could
# mangle the sentinel's exact wording).
SENTINEL_SIN_EVIDENCIAS_TERCERA_PERSONA = "No desarrolló esta obligación a corte del presente informe."

# Obligación sin evidencia en el período, redactada con tacto (origen `sin_labores`) —
# reemplaza los sentinels secos de arriba en escritura nueva. Texto ya impersonal/neutro:
# sirve tanto para primera como tercera persona sin necesidad de una variante separada.
# Deliberadamente NO generado por LLM (ver evidence_justify.py: la rama sin evidencia ya
# evita gastar una llamada LLM porque no hay nada que resumir; añadirla aquí regresaría
# esa invariante para ganar variación de redacción que nadie pidió).
TEXTO_SIN_LABORES = (
    "Durante el periodo reportado no se adelantaron actividades directamente asociadas a "
    "esta obligación; se dará continuidad en periodos subsiguientes conforme a la ejecución "
    "del contrato."
)
