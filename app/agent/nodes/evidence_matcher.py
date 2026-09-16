"""Evidence matcher node — matches evidence to obligations via keyword + LLM (Phase 4)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import TYPE_CHECKING, Any

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.contract_terms import contract_header, contract_number_variants
from app.agent.state import AgentState
from app.core.config import settings
from app.schemas.agent import LLMMessage

if TYPE_CHECKING:
    from app.models.obligacion import Obligacion

logger = structlog.get_logger("agent.nodes.evidence_matcher")

# Keyword-overlap threshold used both as the initial candidate filter and as the
# conservative fallback when the LLM relevance call errors out or returns
# unparseable output (see `_llm_relevance_batch`).
_KEYWORD_THRESHOLD = 0.15
_FALLBACK_ACCEPT_THRESHOLD = 0.30

# Bounds the per-obligación LLM fan-out (radicacion-sin-friccion slice 2.6
# review finding): the parallelized loop below fires one `_llm_relevance_batch`
# call per obligación via `asyncio.gather` — left unbounded, a large contrato
# (dozens of obligaciones) would fire that many SIMULTANEOUS LLM calls, the
# same rate-limit-storm bug class this repo already hit once with Gmail
# (`gmail_adapter.py`'s `_GMAIL_MAX_CONCURRENCY` semaphore) and already guards
# against elsewhere for LLM-adjacent work (`requisito_inference_service.py`'s
# `_INGEST_CONCURRENCY`).
_LLM_FANOUT_CONCURRENCY = 5

_RELEVANCE_BATCH_SYSTEM = """\
Eres un clasificador experto en contratos de prestación de servicios de la función \
pública colombiana. Dada una obligación contractual y una lista numerada de evidencias, \
indica cuáles evidencias demuestran razonablemente el cumplimiento de esa obligación.

CUENTA COMO EVIDENCIA (relevante):
- Informes, actas, entregables, planillas de seguridad social, aprobaciones o vistos buenos.
- Correos con el supervisor o funcionarios de la entidad contratante.
- Reuniones o videollamadas (Meet/Teams) con la entidad, con o sin acta formal.
- Documentos que mencionan el número de contrato, la entidad o el objeto contractual.

NO CUENTA COMO EVIDENCIA (no relevante):
- Boletines, newsletters o notificaciones automáticas sin relación con el contrato.
- Correos personales o de marketing.
- Contenido genérico que no demuestra ninguna actividad del contratista.

Responde ÚNICAMENTE con un array JSON de objetos, uno por evidencia numerada:
[{"idx": 1, "relevante": true, "score": 0.9, "razon": "menciona el informe mensual"}, ...]

- "idx": número de la evidencia (empezando en 1).
- "relevante": true/false.
- "score": qué tan seguro estás de la relevancia, de 0 a 1.
- "razon": explicación breve (una frase).

Si ninguna evidencia es relevante, responde [].
"""

_JSON_RE = re.compile(r"\[.*\]", re.DOTALL)

_CLASIFICAR_SYSTEM = """\
Eres un clasificador. Dada una evidencia y una lista numerada de obligaciones \
contractuales candidatas, indica cuál obligación describe MEJOR esa evidencia.

Responde SOLO con el número de la obligación, o 0 si NINGUNA obligación aplica \
realmente a esta evidencia. Ejemplo: 2.
"""
_NUMBER_RE = re.compile(r"\d+")


async def _clasificar_via_llm(
    texto_evidencia: str, obligaciones: list[Obligacion], llm, fallback: Obligacion | None
) -> Obligacion | None:
    """One LLM call: pick the best-matching obligación out of `obligaciones`, or None.

    `fallback` is what's returned on LLM error or unparseable output — the
    caller passes the strongest keyword-score candidate when at least one
    already cleared `_KEYWORD_THRESHOLD` (weak-but-real signal to fall back
    on), or `None` when nothing did (the LLM is the ONLY signal, so a failure
    there must not fabricate a match). An explicit "0" answer from the model
    (out of range) also falls through to `fallback`, so it naturally resolves
    to None in the zero-keyword-candidate case.
    """
    listado = "\n".join(f"{i + 1}. {ob.descripcion[:400]}" for i, ob in enumerate(obligaciones))
    prompt = (
        f"Evidencia:\n{texto_evidencia[:800]}\n\n"
        f"Obligaciones candidatas:\n{listado}\n\n"
        "¿Cuál obligación describe mejor esta evidencia? Responde solo el número, o 0 si ninguna aplica."
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=_CLASIFICAR_SYSTEM),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            # groq/openai/gpt-oss-20b is a reasoning model: reasoning_tokens count
            # against max_tokens before any visible output. 8 empirically returned
            # EMPTY content (finish_reason=length) even with reasoning_effort="low" —
            # 40 gives real headroom above the single-digit answer this prompt asks for.
            max_tokens=40,
            reasoning_effort="low",
        )
        match = _NUMBER_RE.search(resp.content)
        if not match:
            return fallback
        idx = int(match.group(0)) - 1
        if 0 <= idx < len(obligaciones):
            return obligaciones[idx]
        return fallback
    except Exception:
        return fallback


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """In-memory cosine similarity between two embedding vectors (numpy, no persistence)."""
    import numpy as np

    va, vb = np.array(a, dtype=float), np.array(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _blended_score(keyword_score: float, cosine_score: float | None) -> float:
    """Combine keyword overlap and cosine similarity: embeddings AUGMENT, never
    replace, keyword scoring (evidence-embeddings: Keyword and cosine blended score).
    `cosine_score=None` means embeddings were unavailable this run (fail-open) —
    the blended score degrades to the keyword-only score, unchanged."""
    if cosine_score is None:
        return keyword_score
    return max(keyword_score, cosine_score)


def confidence_bucket(score: float) -> str:
    """Map a blended keyword+cosine score to a qualitative confidence tier —
    "alta" auto-confirms a link, "media"/"baja" persist as proposed
    (evidence-obligation-links: Qualitative confidence levels). Thresholds are
    the EVIDENCE_EMBED_ALTA/MEDIA knobs deferred from Phase 1/2, wired here as
    the first real consumer (Phase 3)."""
    if score >= settings.EVIDENCE_EMBED_ALTA:
        return "alta"
    if score >= settings.EVIDENCE_EMBED_MEDIA:
        return "media"
    return "baja"


async def _embed_batch(texts: list[str], llm) -> list[list[float]] | None:
    """Embed a batch of texts once; fail-open returns None on ANY error (including
    a test double / older LLMPort implementation without `embed()`), so the caller
    degrades to keyword-only ranking instead of failing the run
    (evidence-embeddings: Fail-open to keyword ranking)."""
    if not texts:
        return []
    try:
        return await llm.embed(texts)  # type: ignore[no-any-return]
    except Exception as exc:
        logger.warning("evidence_matcher_embed_failed", error=str(exc))
        return None


def _obligacion_text(ob: Any) -> str:
    """Extract the descripcion/texto text used for matching, from a dict/str/other obligación shape."""
    if isinstance(ob, dict):
        return ob.get("descripcion") or ob.get("texto") or json.dumps(ob, ensure_ascii=False)
    if isinstance(ob, str):
        return ob
    return str(ob)


# Letters/digits + embedded dots/hyphens (min length 3): the old letters-only
# `{4,}` regex could never match a contract number (all digits/dots) or a
# hyphenated radicado code — root cause #2, evidencias/discovery-fix. Dots/
# hyphens are only allowed AFTER the first character, so trailing punctuation
# in prose ("actividades.") still tokenizes as "actividades" plus a lone "."
# that itself never satisfies the {2,} minimum.
_TOKEN_RE = re.compile(r"[a-z0-9áéíóúñü][a-z0-9áéíóúñü.\-]{2,}")


def _keyword_score(obligation_text: str, evidence_text: str) -> float:
    """Simple keyword overlap score between obligation and evidence."""
    if not obligation_text or not evidence_text:
        return 0.0

    ob_words = set(_TOKEN_RE.findall(obligation_text.lower()))
    ev_words = set(_TOKEN_RE.findall(evidence_text.lower()))

    if not ob_words:
        return 0.0
    overlap = ob_words & ev_words
    return len(overlap) / len(ob_words)


def _contains_contract_number(evidence: dict, numero_variants: list[str]) -> bool:
    """True if the evidence's title/content mentions ANY contract-number
    variant (evidencias/discovery-fix WU4)."""
    if not numero_variants:
        return False
    text = f"{evidence.get('title', '')} {evidence.get('content', '')}".lower()
    return any(variant.lower() in text for variant in numero_variants)


# Generic evidence-deliverable terms — an attachment/document whose name
# contains one of these, mentioning the contract number, is treated as an
# auto-match (see `_is_informe_like_document`).
_INFORME_LIKE_TERMS = ("informe", "acta", "entrega", "soporte", "reporte", "planilla")

# Capped bonus added to a candidate's blended score when it mentions the
# contract number but ISN'T a clear document match (evidencias/discovery-fix
# WU7 ranking item c): a number-only hit still needs the LLM's verdict unless
# it's an attachment with an informe-like name — the bonus alone must never
# be enough to clear a normal LLM review, only to make sure it's RANKED and
# SEEN by the LLM (candidates_scored/TOP_N) instead of silently dropped.
_NUMBER_MATCH_BONUS = 0.4


def _is_informe_like_document(evidence: dict) -> bool:
    """True if the evidence is a real attachment/document (not just email body
    prose) whose title/filename reads like a contractual deliverable — the
    narrow case where a contract-number hit is trusted enough to bypass the
    LLM verdict entirely (evidencias/discovery-fix WU7 ranking item c)."""
    is_document = (
        bool(evidence.get("attachment_id")) or bool(evidence.get("file_id")) or evidence.get("source") == "drive"
    )
    if not is_document:
        return False
    title = str(evidence.get("title") or evidence.get("filename") or "").lower()
    return any(term in title for term in _INFORME_LIKE_TERMS)


def _fallback_flags(keyword_scores: list[float] | None, n: int) -> list[bool]:
    """Conservative fallback when the LLM call errors or returns unparseable output.

    Rather than failing fully closed (dropping every candidate for the obligación —
    "minimum effort"), accept candidates whose keyword overlap already clears a
    stricter deterministic bar (>= 0.30, double the initial 0.15 filter). Garbage
    LLM output must NOT accept low-score candidates: if no keyword_scores were
    provided, or none clear the bar, this still returns all-False.
    """
    if not keyword_scores:
        return [False] * n
    return [score >= _FALLBACK_ACCEPT_THRESHOLD for score in keyword_scores]


def _parse_relevance_response(raw: str, n: int) -> list[bool] | None:
    """Parse the LLM's relevance-batch answer, tolerant of two shapes:

    1. NEW structured format (evidencias/discovery-fix WU5): a JSON array of
       `{"idx": int, "relevante": bool, "score": 0-1, "razon": str}` objects.
       An item is kept only when `relevante` is true AND its `score` (when
       present) clears `settings.EVIDENCE_RELEVANCE_MIN`.
    2. LEGACY format: a flat JSON array of relevant indices, e.g. `[1, 3]` —
       kept for backward compatibility with older/smaller models that don't
       follow the structured contract.

    Returns `None` (never a list) when the response has no parseable JSON at
    all — the caller falls back to the deterministic keyword-score bar.
    """
    match = _JSON_RE.search(raw or "")
    if not match:
        return None
    try:
        items = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    if not items:
        return [False] * n

    if all(isinstance(item, dict) for item in items):
        flags = [False] * n
        threshold = settings.EVIDENCE_RELEVANCE_MIN
        for item in items:
            idx = item.get("idx")
            if not isinstance(idx, (int, float)):
                continue
            i = int(idx) - 1
            if not (0 <= i < n):
                continue
            score = item.get("score")
            score_ok = not isinstance(score, (int, float)) or score >= threshold
            flags[i] = bool(item.get("relevante", False)) and score_ok
        return flags

    # Legacy flat int-list format.
    relevant_idx = {int(item) for item in items if isinstance(item, (int, float))}
    return [(i + 1) in relevant_idx for i in range(n)]


async def _llm_relevance_batch(
    obligation: str,
    evidences: list[str],
    llm,
    keyword_scores: list[float] | None = None,
    contrato_contexto: dict | None = None,
) -> list[bool]:
    """Classify all candidate evidences for one obligation in a SINGLE LLM call.

    Returns a boolean per evidence (same order). On error or unparseable output,
    falls back to `_fallback_flags` (deterministic keyword-score bar) instead of
    dropping every candidate — see module docstring / `_FALLBACK_ACCEPT_THRESHOLD`.
    """
    if not evidences:
        return []

    header = contract_header(contrato_contexto)
    listado = "\n".join(f"{i + 1}. {ev[:1500]}" for i, ev in enumerate(evidences))
    prompt = (
        (f"{header}\n\n" if header else "")
        + f"Obligación: {obligation[:800]}\n\n"
        + f"Evidencias:\n{listado}\n\n"
        + "Clasifica cada evidencia según las instrucciones."
    )
    try:
        resp = await llm.complete(
            [
                LLMMessage(role="system", content=_RELEVANCE_BATCH_SYSTEM),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.0,
            # groq/openai/gpt-oss-20b is a reasoning model — reasoning_tokens count
            # against max_tokens before the visible JSON array output. Raised from
            # 120 to 800 (evidencias/discovery-fix WU5): the structured
            # {"idx","relevante","score","razon"} output is far larger per-item
            # than the old bare-int-array contract and needs real headroom.
            max_tokens=800,
            reasoning_effort="low",
        )
        flags = _parse_relevance_response(resp.content, len(evidences))
        if flags is None:
            return _fallback_flags(keyword_scores, len(evidences))
        return flags
    except Exception as exc:
        logger.warning(
            "evidence_matcher_llm_relevance_failed",
            error=str(exc),
            n_evidences=len(evidences),
        )
        return _fallback_flags(keyword_scores, len(evidences))


async def clasificar_evidencia(texto_evidencia: str, obligaciones: list[Obligacion], llm=None) -> Obligacion | None:
    """Best-matching obligación for one evidence text, or None if nothing matches.

    Scores every obligación's `descripcion` against `texto_evidencia` with the same
    `_keyword_score` used by the batch matcher above, and keeps candidates that clear
    `_KEYWORD_THRESHOLD`. Exactly one -> returned directly, no LLM call needed. Two or
    more -> ONE LLM call picks the single best match, falling back to the highest
    keyword-score candidate on LLM failure or unparseable/out-of-range output.

    Zero candidates -> the evidence shares no vocabulary with any obligación's Spanish
    wording, which is common for source code/config/log files whose real content (what
    the code DOES) doesn't echo the contract's phrasing at all. Rather than giving up,
    ONE LLM call judges the evidence against ALL obligaciones semantically (mirrors
    `evidence_matcher_node`'s own zero-candidate fallback) — the model may answer "0"
    (none apply) so genuinely unrelated evidence still lands as unclassified instead of
    being forced onto an arbitrary obligación.
    """
    if not obligaciones or not texto_evidencia:
        return None

    scored = [(ob, _keyword_score(ob.descripcion, texto_evidencia)) for ob in obligaciones]
    candidates = [(ob, score) for ob, score in scored if score >= _KEYWORD_THRESHOLD]

    if llm is None:
        llm = get_llm(model=settings.LLM_EVIDENCE_CLASSIFIER_MODEL)

    if not candidates:
        return await _clasificar_via_llm(texto_evidencia, obligaciones, llm, fallback=None)
    if len(candidates) == 1:
        return candidates[0][0]

    candidates.sort(key=lambda pair: pair[1], reverse=True)
    fallback = candidates[0][0]
    return await _clasificar_via_llm(texto_evidencia, [ob for ob, _score in candidates], llm, fallback=fallback)


async def _match_una_obligacion(
    ob_id: str,
    ob_text: str,
    ob_vec: list[float] | None,
    evidence_raw: list[dict],
    ev_embeddings: list[list[float]] | None,
    llm: Any,
    sem: asyncio.Semaphore,
    numero_variants: list[str] | None = None,
    contrato_contexto: dict | None = None,
) -> tuple[str, list[dict], dict[str, float]]:
    """Resolve matches for ONE obligación — the per-obligación body of the
    (now-parallelized) loop in `evidence_matcher_node`. Touches NO shared
    mutable state beyond its own arguments/return value (no DB access at all
    in this node) and does exactly one `_llm_relevance_batch` LLM call at
    most — safe to run concurrently with the same call for every other
    obligación via `asyncio.gather` (radicacion-sin-friccion Phase 2 slice 2.6).
    The LLM call itself is bounded by `sem` (`_LLM_FANOUT_CONCURRENCY`) so a
    large contrato doesn't fire dozens of simultaneous provider calls.
    """

    def _score(idx: int, ev: dict, bonus: float = 0.0) -> float:
        kw = _keyword_score(ob_text, ev.get("content", ""))
        cos = None
        if ob_vec is not None and ev_embeddings is not None:
            cos = _cosine_similarity(ob_vec, ev_embeddings[idx])
        return min(1.0, _blended_score(kw, cos) + bonus)

    # Step 0: contract-number signal (evidencias/discovery-fix WU4, refined by
    # WU7 ranking item c). An attachment/document with an informe-like name
    # (see `_is_informe_like_document`) mentioning the contract number is a
    # near-certain match: score it 1.0 and skip BOTH the keyword threshold
    # AND the LLM relevance call entirely. Any OTHER evidence mentioning the
    # number (plain email prose, no document support) gets a capped bonus
    # instead — boosted, but the LLM still has the last word, so a number-only
    # mention without real semantic support doesn't auto-qualify.
    auto_matched: list[dict] = []
    scores_dict: dict[str, float] = {}
    scoreable: list[tuple[int, dict, float]] = []
    for idx, ev in enumerate(evidence_raw):
        has_number = _contains_contract_number(ev, numero_variants or [])
        if has_number and _is_informe_like_document(ev):
            item = dict(ev)
            item.setdefault("matched_by", "numero_contrato")
            auto_matched.append(item)
            if "id" in ev:
                scores_dict[ev["id"]] = 1.0
        else:
            scoreable.append((idx, ev, _NUMBER_MATCH_BONUS if has_number else 0.0))

    # Step 1: blended-score filter (≥0.15 threshold) — cosine can surface a
    # candidate keyword scoring alone would miss entirely (cross-language match).
    scored_all = [(ev, _score(idx, ev, bonus)) for idx, ev, bonus in scoreable]

    if len(scored_all) > settings.EVIDENCE_MAX_CANDIDATES_FOR_LLM:
        # Large candidate pool (e.g. many expanded-phrase-query hits,
        # evidencias/discovery-fix WU7): re-apply the keyword pre-gate so an
        # obligación doesn't fan out an unbounded LLM candidate list.
        candidates_scored = [(ev, s) for ev, s in scored_all if s >= _KEYWORD_THRESHOLD]

        # Max-effort fallback: an obligación with ZERO candidates above threshold
        # would otherwise stay silently empty. Instead, take its best candidates
        # with ANY positive score (score > 0) and still let the LLM judge them —
        # better an obligación gets a weak-but-checked candidate than none at all.
        if not candidates_scored:
            positive = [(ev, s) for ev, s in scored_all if s > 0]
            positive.sort(key=lambda pair: pair[1], reverse=True)
            candidates_scored = positive[:3]
    else:
        # Small pool: skip the keyword pre-gate entirely — a candidate found
        # ONLY via an expanded semantic phrase query may share zero keywords
        # with the obligación's own wording and must still reach the LLM
        # (evidencias/discovery-fix WU7). `EVIDENCE_MATCHER_TOP_N` below still
        # bounds the actual LLM fan-out.
        candidates_scored = list(scored_all)

    # Step 2: LLM relevance on top-N candidates — ONE batched call, not one per candidate
    matched_list = list(auto_matched)
    if candidates_scored:
        candidates_scored = sorted(candidates_scored, key=lambda pair: pair[1], reverse=True)[
            : settings.EVIDENCE_MATCHER_TOP_N
        ]
        candidates = [ev for ev, _s in candidates_scored]
        blended_scores = [s for _ev, s in candidates_scored]
        async with sem:
            flags = await _llm_relevance_batch(
                ob_text, [ev.get("content", "") for ev in candidates], llm, blended_scores, contrato_contexto
            )
        matched_list.extend(ev for ev, keep in zip(candidates, flags, strict=True) if keep)
        # Additive: the blended score behind each KEPT match, keyed by the
        # evidence's own "id" field (present for local-upload evidence_raw
        # dicts; simply absent/skipped for sources that don't set one, e.g.
        # Google discovery — those flows don't consume this key).
        scores_dict.update(
            {
                ev["id"]: score
                for ev, score, keep in zip(candidates, blended_scores, flags, strict=True)
                if keep and "id" in ev
            }
        )

    return ob_id, matched_list, scores_dict


async def evidence_matcher_node(state: AgentState) -> AgentState:
    """Match evidence to obligations using keyword score + LLM refinement.

    Reads: evidence_raw, obligaciones_extraidas
    Writes: matched_evidence, current_phase
    """
    evidence_raw: list[dict] = state.get("evidence_raw") or []
    obligaciones: list = state.get("obligaciones_extraidas") or []

    if not obligaciones or not evidence_raw:
        return {
            **state,
            "matched_evidence": {},
            "matched_evidence_scores": {},
            "current_phase": "evidence_matcher",
        }

    llm = get_llm(model=settings.LLM_EVIDENCE_CLASSIFIER_MODEL)
    matched: dict[str, list[dict]] = {}
    matched_scores: dict[str, dict[str, float]] = {}

    contrato_contexto = state.get("contrato_contexto") or {}
    numero_variants = contract_number_variants(contrato_contexto.get("numero_contrato"))

    # Embed once for the whole run (per-contrato reuse, not once per evidence file):
    # one batch call for every obligación text, one for every evidence text. Skips
    # the evidence batch entirely if the obligación batch already failed, and
    # fails open to None (keyword-only ranking) on any embedding error.
    ob_texts = [_obligacion_text(ob) for ob in obligaciones]
    ev_texts = [ev.get("content", "") for ev in evidence_raw]
    # Embedding input ONLY (evidencias/discovery-fix WU7 ranking item a) — the
    # contract's objeto qualifies a short/generic obligación phrase (e.g.
    # "supervisar cronograma") with what the whole contract is actually
    # about, which meaningfully changes its semantic embedding. `ob_texts`
    # itself stays bare — it's still used for keyword scoring and the LLM
    # candidate listing, which shouldn't be diluted by the objeto text.
    objeto = str(contrato_contexto.get("objeto") or "").strip()
    ob_embed_texts = [f"{objeto} {t}".strip() if objeto else t for t in ob_texts]
    ob_embeddings = await _embed_batch(ob_embed_texts, llm)
    ev_embeddings = await _embed_batch(ev_texts, llm) if ob_embeddings is not None else None

    # Every obligación's matching runs CONCURRENTLY (radicacion-sin-friccion
    # Phase 2 slice 2.6) — this loop touches no DB session and no shared
    # mutable state across iterations (see `_match_una_obligacion`'s
    # docstring), so it's safe to fan out with `asyncio.gather` instead of
    # awaiting one obligación's LLM call at a time. `asyncio.gather` preserves
    # input order in its results regardless of completion order, so building
    # `matched`/`matched_scores` from the results afterward needs no extra
    # bookkeeping for out-of-order completions.
    sem = asyncio.Semaphore(_LLM_FANOUT_CONCURRENCY)
    tareas = []
    for i, ob in enumerate(obligaciones):
        ob_text = ob_texts[i]
        ob_id = ob.get("id") if isinstance(ob, dict) else str(i)
        if not ob_id:
            ob_id = str(i)
        ob_vec = ob_embeddings[i] if ob_embeddings is not None else None
        tareas.append(
            _match_una_obligacion(
                str(ob_id), ob_text, ob_vec, evidence_raw, ev_embeddings, llm, sem, numero_variants, contrato_contexto
            )
        )

    for ob_id, matched_list, scores_dict in await asyncio.gather(*tareas):
        matched[ob_id] = matched_list
        matched_scores[ob_id] = scores_dict

    total_matched = sum(len(v) for v in matched.values())
    await logger.ainfo(
        "evidence_matcher_done",
        n_obligations=len(obligaciones),
        total_matched=total_matched,
    )

    return {
        **state,
        "matched_evidence": matched,
        "matched_evidence_scores": matched_scores,
        "current_phase": "evidence_matcher",
    }
