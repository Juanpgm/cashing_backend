"""Evidence matcher node — matches evidence to obligations via keyword + LLM (Phase 4)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import TYPE_CHECKING, Any

import structlog

from app.adapters.llm import get_llm
from app.agent.prompts.contract_terms import (
    OBJETO_EMBED_MAX_CHARS,
    contract_header,
    contract_match_variants,
    contract_number_variants,
    contract_query_variants,
)
from app.agent.prompts.email_evidence import _STOPWORDS
from app.agent.prompts.query_budget import obligacion_key
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
        vectors = await llm.embed(texts)
    except Exception as exc:
        logger.warning("evidence_matcher_embed_failed", error=str(exc))
        return None
    # `_score` indexes `ev_embeddings[idx]` positionally, so a provider that
    # returns fewer vectors than inputs raised IndexError out of the gather and
    # surfaced as a 500. Fail open to keyword-only ranking instead.
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        logger.warning(
            "evidence_matcher_embed_length_mismatch",
            expected=len(texts),
            received=len(vectors) if isinstance(vectors, list) else None,
        )
        return None
    return vectors


def _obligacion_text(ob: Any) -> str:
    """Extract the descripcion/texto text used for matching, from a dict/str/other obligación shape."""
    if isinstance(ob, dict):
        return ob.get("descripcion") or ob.get("texto") or json.dumps(ob, ensure_ascii=False)
    if isinstance(ob, str):
        return ob
    return str(ob)


# TWO tokenizers, not one (round-2 fix for a confirmed CRITICAL finding).
#
# The single `[a-z0-9áéíóúñü][a-z0-9áéíóúñü.\-]{2,}` pattern had two defects
# that together destroyed the keyword signal:
#
# (a) minimum length 3 made "los"/"del"/"por"/"que"/"con" scoring tokens, so
#     ANY Spanish text overlapped ANY obligación. Measured: a real deliverable
#     and an unrelated marketing newsletter both scored 0.2308 — the matcher
#     could not rank evidence above spam, and plain conversational Spanish
#     scored 0.3846, clearing `_FALLBACK_ACCEPT_THRESHOLD` so it was emitted as
#     evidence with no LLM verdict whenever the provider errored;
# (b) `.`/`-` inside the class with greedy matching glued trailing punctuation:
#     "informe." tokenized as "informe." and did NOT match "informe", halving
#     genuine matches. (The old docstring claimed the opposite; it was wrong.)
#
# Prose is now letters-only with a {4,} floor and the shared Spanish stopword
# list applied. Identifiers (contract numbers, radicado codes) get their own
# pattern: they must contain a digit and be >= 5 chars after leading/trailing
# `.`/`-`/`/` are stripped — which keeps root cause #2 fixed (a dotted contract
# number is still a first-class scoring token) without admitting stopwords.
_PROSE_TOKEN_RE = re.compile(r"[a-záéíóúñü]{4,}")
_IDENT_TOKEN_RE = re.compile(r"[a-z0-9áéíóúñü][a-z0-9áéíóúñü.\-/]*[a-z0-9áéíóúñü]")
_MIN_IDENT_LEN = 5


def _tokens(text: str) -> set[str]:
    """Scoring tokens: content-bearing prose words + identifier-like codes."""
    lowered = text.lower()
    out = {w for w in _PROSE_TOKEN_RE.findall(lowered) if w not in _STOPWORDS}
    for candidate in _IDENT_TOKEN_RE.findall(lowered):
        token = candidate.strip(".-/")
        if len(token) >= _MIN_IDENT_LEN and any(c.isdigit() for c in token):
            out.add(token)
    return out


def _keyword_score(obligation_text: str, evidence_text: str) -> float:
    """Simple keyword overlap score between obligation and evidence."""
    if not obligation_text or not evidence_text:
        return 0.0

    ob_words = _tokens(obligation_text)
    ev_words = _tokens(evidence_text)

    if not ob_words:
        return 0.0
    overlap = ob_words & ev_words
    return len(overlap) / len(ob_words)


def _mentions_any(evidence: dict[str, object], variants: list[str]) -> bool:
    """Word-boundary search for any of `variants` in the evidence text.

    A plain `variant in text` substring test made `ORD-41612` a hit for the
    variant `4161` (confirmed CRITICAL finding). The lookarounds below reject a
    match that is glued to another alphanumeric character.
    """
    if not variants:
        return False
    text = f"{evidence.get('title', '')} {evidence.get('content', '')}".lower()
    return any(re.search(rf"(?<![0-9A-Za-z]){re.escape(variant.lower())}(?![0-9A-Za-z])", text) for variant in variants)


def _contains_contract_number(evidence: dict, numero_variants: list[str]) -> bool:
    """True if the evidence mentions a TRUSTWORTHY contract-number variant.

    Weak derived forms (the bare dependency code `4161`, the `027-2025`
    last-pair) are filtered out by `contract_match_variants` — on their own they
    identify the entity or the year, not this contract.
    """
    return _mentions_any(evidence, contract_match_variants_of(numero_variants))


def _has_strong_contract_number(evidence: dict[str, object], numero_variants: list[str]) -> bool:
    """True only for the FULL number (raw or hyphen-normalized).

    This is the high-specificity signal: a different contract of the same
    entity ("4161.010.26.1.099.2025") must never satisfy it.
    """
    if not numero_variants:
        return False
    raw = numero_variants[0]
    return _mentions_any(evidence, contract_query_variants(raw))


def contract_match_variants_of(numero_variants: list[str]) -> list[str]:
    """`contract_match_variants` re-derived from an already-expanded list, so
    callers that only hold the variant list don't need the raw number."""
    if not numero_variants:
        return []
    return contract_match_variants(numero_variants[0])


# Generic evidence-deliverable terms — an attachment/document whose name
# contains one of these, mentioning the contract number, earns the strongest
# reserved slot in the LLM slate (see `_is_informe_like_document`).
_INFORME_LIKE_TERMS = ("informe", "acta", "entrega", "soporte", "reporte", "planilla")


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
    all — the caller falls back to the deterministic keyword-score bar. Every
    failure to parse silently DISABLES the LLM noise filter the product depends
    on, so extraction is deliberately tolerant (round-2 confirmed WARNING).
    """
    items = _extract_json_array(raw or "")
    if items is None:
        return None
    if not items:
        return [False] * n

    flags = [False] * n
    saw_structured = False
    legacy_idx: set[int] = set()

    # Per-item handling, NOT all-or-nothing: a single stray int used to send the
    # whole array down the legacy branch and discard every dict entry.
    for item in items:
        if isinstance(item, dict):
            saw_structured = True
            idx = item.get("idx")
            if not isinstance(idx, (int, float)) or isinstance(idx, bool):
                continue
            i = int(idx) - 1
            if not (0 <= i < n):
                continue
            flags[i] = _is_relevante(item.get("relevante", False)) and _score_ok(item.get("score"))
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            legacy_idx.add(int(item))

    # Legacy flat int-list format (older/smaller models): only when NO
    # structured object was present, so a mixed array keeps the rich verdicts.
    if not saw_structured:
        return [(i + 1) in legacy_idx for i in range(n)]
    for i in legacy_idx:
        if 0 <= i - 1 < n:
            flags[i - 1] = True
    return flags


_TRUTHY_RELEVANTE = {"true", "si", "sí", "1", "yes", "verdadero"}


def _is_relevante(value: object) -> bool:
    """Coerce `relevante` explicitly. `bool("false")` is True in Python, so the
    old `bool(item.get("relevante"))` accepted the JSON string "false" as a
    positive verdict (round-2 confirmed WARNING)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_RELEVANTE
    if isinstance(value, (int, float)):
        return value > 0
    return False


def _score_ok(score: object) -> bool:
    """Apply `EVIDENCE_RELEVANCE_MIN` by VALUE, not by type.

    `not isinstance(score, (int, float)) or score >= threshold` let a quoted
    "0.1" and an unnormalized 90 pass while rejecting an honest 0.4. A numeric
    string is now parsed, and a 0-100 percentage is normalized before the
    comparison. An absent/null/unparseable score stays tolerated — the rubric
    documents the field as checked "when present" for legacy-model compat.

    SUGGESTION fix (round-3): `if value > 1.0: value /= 100.0` treated EVERY
    out-of-range value as a percentage, so a model drifting to a 0-10
    confidence scale (the prompt only says "de 0 a 1", a common miscalibration
    for small models) got silently divided by 100 too — an honest 8/10 became
    0.08 and a genuine `relevante: true` verdict was discarded with no
    recovery path (a real list, not None, so the keyword-score fallback never
    fires either). Values normalize only when unambiguously a percentage
    (> 10); a value in the ambiguous (1, 10] range is now treated the same as
    an absent/unparseable score — tolerated, letting `relevante` decide —
    rather than confidently misinterpreted as a percentage.
    """
    if score is None or isinstance(score, bool):
        return True
    if isinstance(score, str):
        try:
            score = float(score.strip().rstrip("%"))
        except ValueError:
            return True
    if not isinstance(score, (int, float)):
        return True
    value = float(score)
    if value > 10.0:
        value /= 100.0
    elif value > 1.0:
        return True
    return value >= settings.EVIDENCE_RELEVANCE_MIN


def _extract_json_array(raw: str) -> list[object] | None:
    """Find the response's JSON array, tolerant of the shapes models actually emit.

    `re.search(r"\\[.*\\]", DOTALL)` is greedy across the WHOLE response, so a
    reasoning preamble mentioning "[1]", a trailing "Nota [1]: ..." or a
    max_tokens-truncated array all defeated it. This walks bracket depth from
    EVERY candidate opening bracket (not just the first) and, failing a clean
    parse, salvages the complete objects from a truncated array rather than
    discarding the verdict entirely.

    Round-2 REGRESSION fix (confirmed CRITICAL): taking the FIRST balanced
    array meant a reasoning preamble like "los items [2, 3] parecen
    relevantes" — routine output from a reasoning model such as
    `groq/openai/gpt-oss-20b` — was returned instead of the real structured
    verdict that follows it, silently INVERTING the model's answer. Every
    balanced array in the text is now collected, and an array whose items are
    OBJECTS (the structured `{"idx", "relevante", ...}` contract) is always
    preferred over a bare list of ints, taking the LAST such array so a
    trailing "Nota [1]: ..." bracket doesn't win either. A bare int array is
    only trusted when no object array exists anywhere in the response.
    """
    text = re.sub(r"```(?:json)?|```", "", raw).strip()

    candidates: list[list[object]] = []
    for start in (m.start() for m in re.finditer(r"\[", text)):
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, len(text)):
            ch = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : pos + 1])
                    except (ValueError, TypeError):
                        pass
                    else:
                        if isinstance(parsed, list):
                            candidates.append(parsed)
                    break
        else:
            # Ran off the end with brackets still open: the array was truncated.
            salvaged = _salvage_truncated_array(text[start:])
            if salvaged is not None:
                candidates.append(salvaged)

    if not candidates:
        return None
    object_candidates = [c for c in candidates if any(isinstance(item, dict) for item in c)]
    if object_candidates:
        return object_candidates[-1]
    return candidates[-1]


def _salvage_truncated_array(fragment: str) -> list[object] | None:
    """Recover the complete top-level objects from an array cut off mid-item."""
    items: list[object] = []
    depth = 0
    in_string = False
    escaped = False
    obj_start = -1
    for pos, ch in enumerate(fragment):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                obj_start = pos
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and obj_start >= 0:
                with contextlib.suppress(ValueError, TypeError):
                    items.append(json.loads(fragment[obj_start : pos + 1]))
                obj_start = -1
    return items or None


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

    def _score(idx: int, ev: dict) -> tuple[float, float]:
        """Returns (keyword_score, blended_score) — PURE similarity, no
        contract-number bonus. `blended_score` feeds `confidence_bucket`, so it
        must stay a similarity measure; the number signal is applied as a
        reserved slot in the ranking instead. `keyword_score` is kept SEPARATE
        (round-2 CRITICAL fix): the LLM-failure fallback bar
        (`_FALLBACK_ACCEPT_THRESHOLD`) is documented as keyword overlap, so it
        must never be compared against the cosine-inflated blended value."""
        kw = _keyword_score(ob_text, ev.get("content", ""))
        cos = None
        if ob_vec is not None and ev_embeddings is not None:
            cos = _cosine_similarity(ob_vec, ev_embeddings[idx])
        return kw, min(1.0, _blended_score(kw, cos))

    # Step 0: score everything on its OWN semantic merit, and record the
    # contract-number signal separately as a RESERVATION priority rather than
    # folding it into the score.
    #
    # Round-2 fix for two confirmed CRITICAL findings:
    #  - an informe-like document with a number hit used to be auto-matched at
    #    score 1.0 with the LLM skipped. Because this body runs once per
    #    obligación, ONE Drive file attached itself to EVERY obligación of the
    #    contract — and deciding WHICH obligaciones a deliverable covers is
    #    exactly the LLM's job;
    #  - the flat `+0.4` bonus was added BEFORE the TOP_N cut, so 50 items whose
    #    only link was a 4-digit prefix filled all 8 LLM slots and the genuine
    #    semantic match never reached the model.
    #
    # The number now buys a GUARANTEED SLOT, not a higher score: the reported
    # score stays a pure similarity measure (it feeds `confidence_bucket`), and
    # the LLM still decides relevance.
    scores_dict: dict[str, float] = {}
    scored_all: list[tuple[dict, float, int, float]] = []
    for idx, ev in enumerate(evidence_raw):
        if _has_strong_contract_number(ev, numero_variants or []):
            priority = 2 if _is_informe_like_document(ev) else 1
        elif _contains_contract_number(ev, numero_variants or []):
            priority = 1
        else:
            priority = 0
        kw_score, blended = _score(idx, ev)
        scored_all.append((ev, blended, priority, kw_score))

    # Step 1: RANK-based selection, never threshold-based.
    #
    # The old code re-applied the 0.15 keyword pre-gate once the pool exceeded
    # EVIDENCE_MAX_CANDIDATES_FOR_LLM, and its rescue only kept items scoring
    # > 0 — so an all-zero pool produced an EMPTY candidate list and the LLM was
    # never called. Measured: pool=40 -> 8 matches, pool=41 -> 0 matches. That
    # made discovery non-monotonic in the amount of evidence found. TOP_N
    # already bounds the LLM fan-out, so the pre-gate bought no cost control at
    # all; taking the best TOP_N by rank can never produce an empty slate.
    top_n = settings.EVIDENCE_MATCHER_TOP_N
    reserved_n = min(settings.EVIDENCE_NUMBER_RESERVED_SLOTS, top_n)

    by_score = sorted(range(len(scored_all)), key=lambda i: -scored_all[i][1])
    # Only reserve a slot for a priority hit that is EITHER an informe-like
    # document (priority 2 — trusted regardless of score) OR carries some
    # actual semantic/keyword signal (score > 0). Round-2 WARNING fix: a bare
    # priority-1 contract-number mention with score 0.0 (an invoice, an SMS
    # notification) used to reserve a slot unconditionally, evicting a
    # strictly-higher-scoring semantic candidate ranked #6-#8 that `by_score`
    # would otherwise have chosen.
    reserved = sorted(
        (i for i in range(len(scored_all)) if scored_all[i][2] > 0 and (scored_all[i][2] == 2 or scored_all[i][1] > 0)),
        key=lambda i: (-scored_all[i][2], -scored_all[i][1]),
    )[:reserved_n]

    chosen: list[int] = list(reserved)
    reserved_set = set(reserved)
    for i in by_score:
        if len(chosen) >= top_n:
            break
        if i not in reserved_set:
            chosen.append(i)

    candidates_scored = sorted(
        ((scored_all[i][0], scored_all[i][1], scored_all[i][3]) for i in chosen), key=lambda p: -p[1]
    )

    # Step 2: LLM relevance on the slate — ONE batched call, not one per candidate
    matched_list: list[dict] = []
    if candidates_scored:
        candidates = [ev for ev, _s, _kw in candidates_scored]
        blended_scores = [s for _ev, s, _kw in candidates_scored]
        # keyword_scores (NOT blended_scores) feeds the LLM-failure fallback bar
        # (round-2 CRITICAL fix): `_fallback_flags`'s 0.30 bar is documented as
        # keyword overlap, and comparing it against the cosine-inflated blended
        # score meant the whole slate was accepted on any relevance-LLM error
        # whenever embeddings succeeded, regardless of actual keyword support.
        keyword_scores = [kw for _ev, _s, kw in candidates_scored]
        async with sem:
            flags = await _llm_relevance_batch(
                ob_text, [ev.get("content", "") for ev in candidates], llm, keyword_scores, contrato_contexto
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
    # Merge in the contratista's own monthly summary (evidencias/discovery-fix
    # WU7b) for the shared header WITHOUT mutating `state["contrato_contexto"]`.
    contexto_usuario_val = state.get("contexto_usuario")
    contrato_contexto_for_prompt = (
        {**contrato_contexto, "contexto_usuario": contexto_usuario_val} if contexto_usuario_val else contrato_contexto
    )

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
    objeto = str(contrato_contexto.get("objeto") or "").strip()[:OBJETO_EMBED_MAX_CHARS]
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
    # SUGGESTION fix (round-3): the task loop and the exception-fallback list
    # below used to compute the obligación key via two DIFFERENT expressions
    # (`ob.get("id") or str(i)` vs. `str(ob.get("id") or i)`), which diverge
    # whenever `str(id)` is itself a non-empty-but-"falsy-looking" string —
    # e.g. `id=None` makes `str(None)` the TRUTHY string "None", so the old
    # fallback expression never fell through to `str(i)` and keyed that
    # obligación "None" while the task keyed it "0". Both branches now share
    # the ONE designated derivation, `query_budget.obligacion_key`.
    def _ob_id_for(ob: Any, i: int) -> str:
        return obligacion_key(ob, i) if isinstance(ob, dict) else str(i)

    sem = asyncio.Semaphore(_LLM_FANOUT_CONCURRENCY)
    tareas = []
    for i, ob in enumerate(obligaciones):
        ob_text = ob_texts[i]
        ob_vec = ob_embeddings[i] if ob_embeddings is not None else None
        tareas.append(
            _match_una_obligacion(
                _ob_id_for(ob, i),
                ob_text,
                ob_vec,
                evidence_raw,
                ev_embeddings,
                llm,
                sem,
                numero_variants,
                contrato_contexto_for_prompt,
            )
        )

    # `return_exceptions=True`: one obligación's provider failure must degrade
    # THAT obligación to empty, not abort the whole discovery run. The caller
    # (`evidence_discovery_service.descubrir_evidencias`) invokes this node with
    # no try/except, so a propagated exception became an unhandled 500.
    ob_ids = [_ob_id_for(ob, i) for i, ob in enumerate(obligaciones)]
    for fallback_id, outcome in zip(ob_ids, await asyncio.gather(*tareas, return_exceptions=True), strict=True):
        if isinstance(outcome, BaseException):
            await logger.awarning("evidence_matcher_obligacion_failed", ob_id=fallback_id, error=str(outcome))
            matched[fallback_id] = []
            matched_scores[fallback_id] = {}
            continue
        ob_id, matched_list, scores_dict = outcome
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
