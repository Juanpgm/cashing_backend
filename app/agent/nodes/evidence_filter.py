"""Evidence filter node — descarta ruido antes del matching (trabajo vs. ruido, Phase 4b).

Corre entre evidence_orchestrator y evidence_matcher. Dos capas:
1. Heurísticas deterministas gratis (patrones de remitente/asunto, labels de Gmail,
   metadatos de Calendar, carpetas de Drive).
2. Clasificador LLM batch "TRABAJO/RUIDO" para los items que pasan la capa 1.

Default de agresividad: en caso de duda conserva el item (nunca pierde evidencia válida).
"""

from __future__ import annotations

import structlog

from app.adapters.llm import get_llm
from app.agent.nodes.evidence_matcher import _extract_json_array, _mentions_any, contract_match_variants_of
from app.agent.prompts.contract_terms import contract_header, contract_number_variants
from app.agent.prompts.evidence_filter import (
    WORK_NOISE_SYSTEM_PROMPT,
    build_work_noise_prompt,
    is_noise_calendar,
    is_noise_drive,
    is_noise_ms_calendar,
    is_noise_ms_drive,
    score_non_personal_email,
    score_non_personal_ms_email,
)
from app.agent.state import AgentState
from app.core.config import settings
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.evidence_filter")

_LLM_BATCH_SIZE = 15


async def _llm_classify_batch(
    items: list[dict], llm, contrato_contexto: dict | None = None
) -> list[bool]:  # True = TRABAJO
    """Clasifica un lote de items como TRABAJO o RUIDO vía LLM.

    En caso de error de LLM o parseo, conserva todos los items (safe default).
    Devuelve lista de booleans (True = conservar) con mismo índice que `items`.
    `contrato_contexto` (evidencias/discovery-fix WU5) feeds the shared
    contract header into the prompt.
    """
    if not items:
        return []

    indexed = [
        {
            "idx": i,
            "source": it["source"],
            "title": it["title"],
            "content": it["content"],
            # Round-2: the sender is what makes the contract header actionable
            # (see build_work_noise_prompt) — it was never passed before.
            "sender": (it.get("metadata") or {}).get("sender") or "",
        }
        for i, it in enumerate(items)
    ]
    prompt = build_work_noise_prompt(indexed, header=contract_header(contrato_contexto))

    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=WORK_NOISE_SYSTEM_PROMPT),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            # groq/openai/gpt-oss-20b is a reasoning model: reasoning_tokens count
            # against max_tokens before any visible output. This is the tightest
            # budget of the 4 sites tuned in the groq-fallback-model-decommissioned
            # fix -- _LLM_BATCH_SIZE=15 means the visible output is a JSON array of
            # up to 15 `{"idx": N, "verdict": "TRABAJO"}` objects. Worst case:
            # ~18-20 tokens/object (2-digit idx, "TRABAJO", braces/commas) x 15
            # items ~= 270-300 tokens of visible content alone, before any hidden
            # reasoning tokens. The other 3 sites (single-number and small
            # JSON-array answers) needed roughly 1.9x-5x headroom above their
            # verified-working baseline to absorb reasoning_effort="low"
            # overhead; 256 was never empirically verified here and can be
            # smaller than the worst-case visible content by itself. 700 =
            # ~300 worst-case content + ~400 headroom for reasoning + margin.
            max_tokens=700,
            reasoning_effort="low",
        )
        raw = resp.content or ""
        # Round-3 fix (confirmed WARNING): the greedy `_JSON_RE = r"\[.*\]"`
        # spanned from the FIRST bracket to the LAST bracket in the whole
        # response — a reasoning preamble bracket or a trailing prose bracket
        # (both routine for `LLM_EVIDENCE_CLASSIFIER_MODEL`, a reasoning
        # model) made the span unparseable, and the except below then kept
        # the WHOLE batch (fail open), silently disabling the noise filter
        # the product depends on. Reuses the same tolerant extractor
        # `evidence_matcher._extract_json_array` already uses for the
        # identical JSON-array contract against the same model.
        verdicts = _extract_json_array(raw)
        if verdicts is None:
            raise ValueError("No JSON array in LLM response")
        idx_to_verdict = {int(v["idx"]): v.get("verdict", "TRABAJO") for v in verdicts if isinstance(v, dict)}
    except Exception as exc:
        await logger.awarning("evidence_filter_llm_failed", error=str(exc), batch_size=len(items))
        # Conservar todo el lote si el LLM falla
        return [True] * len(items)

    return [idx_to_verdict.get(i, "TRABAJO") != "RUIDO" for i in range(len(items))]


def _heuristic_is_noise(
    item: dict,
    numero_variants: list[str] | None = None,
    supervisor_domain: str | None = None,
) -> bool:
    """Capa 1: heurísticas deterministas por (source, provider). True = descartar.

    Dispatches to the Microsoft counterpart when `metadata.provider ==
    "microsoft"`; defaults to "google" (the Google heuristics) for legacy
    items that predate the provider marker (microsoft-noise-heuristics spec:
    "each item scored by its own provider's heuristic").

    Round-2 fix (confirmed WARNING): `numero_variants`/`supervisor_domain` were
    wired ONLY at the service pre-filter. This node then re-scored the very same
    emails without them and silently undid the WU4 contract-number rescue one
    layer later — the same message was saved at layer 1 and killed at layer 2.
    Both default to None so every existing caller keeps working.
    """
    source = item.get("source", "")
    meta = item.get("metadata") or {}
    provider = meta.get("provider") or "google"
    is_microsoft = provider == "microsoft"

    if source == "email":
        sender = meta.get("sender") or ""
        labels = meta.get("labels") or []
        title = item.get("title") or ""
        headers = meta.get("headers") or {}
        contains_numero = _mentions_any(item, contract_match_variants_of(numero_variants or []))
        if is_microsoft:
            score, _ = score_non_personal_ms_email(
                sender,
                title,
                categories=labels,
                inference_classification=headers.get("inferenceClassification", ""),
                supervisor_domain=supervisor_domain,
                contains_contract_number=contains_numero,
            )
        else:
            score, _ = score_non_personal_email(
                sender,
                title,
                labels,
                headers,
                supervisor_domain=supervisor_domain,
                contains_contract_number=contains_numero,
            )
        return score >= 3

    if source == "calendar":
        cal_meta = meta.get("metadata") or {}
        title = item.get("title") or ""
        if is_microsoft:
            return is_noise_ms_calendar(title, cal_meta)
        return is_noise_calendar(title, cal_meta)

    if source == "drive":
        mime = meta.get("mime_type") or ""
        if is_microsoft:
            return is_noise_ms_drive(mime)
        return is_noise_drive(mime)

    return False


async def evidence_filter_node(state: AgentState) -> AgentState:
    """Filtra ruido de evidence_raw antes de pasarlo al matcher.

    Reads: evidence_raw
    Writes: evidence_raw (filtrado), evidencias_descartadas
    """
    evidence_raw: list[dict] = state.get("evidence_raw") or []
    if not evidence_raw:
        return {**state, "evidencias_descartadas": 0, "current_phase": "evidence_filter"}

    # Contract-number / supervisor signals, so layer 1 here reaches the SAME
    # verdict as the service pre-filter instead of reverting its rescue
    # (round-2 confirmed WARNING).
    contrato_ctx = state.get("contrato_contexto") or {}
    numero_variants = contract_number_variants(contrato_ctx.get("numero_contrato"))
    supervisor_email = str(contrato_ctx.get("supervisor_email") or "")
    supervisor_domain = supervisor_email.split("@")[-1].strip().lower() if "@" in supervisor_email else None

    # Capa 1: heurísticas deterministas
    after_heuristics: list[dict] = []
    heuristic_dropped = 0
    for item in evidence_raw:
        if _heuristic_is_noise(item, numero_variants, supervisor_domain):
            heuristic_dropped += 1
            await logger.adebug(
                "evidence_filter_heuristic_drop",
                source=item.get("source"),
                title=item.get("title", "")[:80],
            )
        else:
            after_heuristics.append(item)

    # Los archivos subidos por el usuario (local_file) nunca pasan por el
    # clasificador de ruido: subirlos como evidencia YA es intención explícita.
    kept: list[dict] = [it for it in after_heuristics if it.get("source") == "local_file"]
    clasificables = [it for it in after_heuristics if it.get("source") != "local_file"]

    # Capa 2: clasificador LLM batch
    llm = get_llm(model=settings.LLM_EVIDENCE_CLASSIFIER_MODEL)
    llm_dropped = 0

    # Merge in the contratista's own monthly summary (evidencias/discovery-fix
    # WU7b) for the shared header WITHOUT mutating `state["contrato_contexto"]`.
    contrato_contexto = state.get("contrato_contexto") or {}
    contexto_usuario_val = state.get("contexto_usuario")
    contrato_contexto_for_prompt = (
        {**contrato_contexto, "contexto_usuario": contexto_usuario_val} if contexto_usuario_val else contrato_contexto
    )

    for batch_start in range(0, len(clasificables), _LLM_BATCH_SIZE):
        batch = clasificables[batch_start : batch_start + _LLM_BATCH_SIZE]
        keep_flags = await _llm_classify_batch(batch, llm, contrato_contexto_for_prompt)
        for item, keep in zip(batch, keep_flags, strict=True):
            if keep:
                kept.append(item)
            else:
                llm_dropped += 1
                await logger.adebug(
                    "evidence_filter_llm_drop",
                    source=item.get("source"),
                    title=item.get("title", "")[:80],
                )

    total_dropped = heuristic_dropped + llm_dropped
    await logger.ainfo(
        "evidence_filter_done",
        original=len(evidence_raw),
        kept=len(kept),
        dropped_heuristic=heuristic_dropped,
        dropped_llm=llm_dropped,
    )

    return {
        **state,
        "evidence_raw": kept,
        "evidencias_descartadas": total_dropped,
        "current_phase": "evidence_filter",
    }
