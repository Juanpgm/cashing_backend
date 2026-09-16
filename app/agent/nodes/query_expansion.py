"""Semantic query-term expansion node (evidencias/discovery-fix WU7).

`expand_search_terms` asks ONE LLM call for 4-8 search phrases per obligación
(synonyms, expected deliverable names, likely counterpart names, filename
variants) so the Gmail/Drive/Calendar query builders can find evidence that
never mentions the contract number or the obligación's own literal wording.
Falls back to the deterministic `_extract_keywords` per obligación when the
LLM is unavailable, a fake/test double without a usable response, or returns
malformed output — this must never fail the discovery run.
"""

from __future__ import annotations

import asyncio
import json
import re

import structlog

from app.agent.prompts.contract_terms import contract_header
from app.agent.prompts.email_evidence import _extract_keywords
from app.agent.prompts.query_budget import obligacion_key as _obligacion_id
from app.agent.prompts.query_expansion import QUERY_EXPANSION_SYSTEM_PROMPT, build_query_expansion_prompt
from app.core.config import settings
from app.schemas.agent import LLMMessage

logger = structlog.get_logger("agent.nodes.query_expansion")

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_MAX_PHRASES_PER_OBLIGACION = 8


def _bigram_phrases(descripcion: str) -> list[str]:
    """Consecutive-keyword bigrams from `_extract_keywords`'s output.

    Round-3 fix (confirmed WARNING, escalated from SUGGESTION): the
    deterministic fallback used to emit `_extract_keywords`'s bare single
    words — the SAME words `build_obligation_queries` already covers via
    `subject:(a OR b OR c OR d)`. Wrapped individually in quotes by
    `build_expanded_phrase_queries`, each single word became a near-useless
    unscoped full-text query (`"elaborar" after:... before:...` returns
    essentially the mailbox) that burned a guaranteed round-robin slot.
    Bigrams are genuinely more specific than either word alone and survive
    `build_expanded_phrase_queries`'s multi-word filter, so the fallback adds
    real discriminating power instead of duplicating the subject: query.
    A single leftover keyword (<2 keywords total) is kept as-is — the
    multi-word filter downstream will drop it rather than fire a bad query.
    """
    keywords = _extract_keywords(descripcion)
    if len(keywords) < 2:
        return keywords
    return [f"{keywords[i]} {keywords[i + 1]}" for i in range(len(keywords) - 1)]


def _deterministic_terms(obligaciones: list[dict]) -> dict[str, list[str]]:
    """Fallback: bigram phrases derived from the existing keyword extractor,
    per obligación (see `_bigram_phrases`)."""
    return {_obligacion_id(ob, i): _bigram_phrases(str(ob.get("descripcion") or "")) for i, ob in enumerate(obligaciones)}


def _model_credentials_ready(model: str) -> bool:
    """Can the configured model actually be called?

    Expansion is a BEST-EFFORT enhancement whose fallback is already
    deterministic, so it must never pay for a call that cannot succeed.
    `LiteLLMAdapter._call_model` is wrapped in tenacity
    `@retry(stop_after_attempt(2), wait_exponential(min=1, max=4))` and walks a
    3-model fallback chain, so ONE call against an unauthenticated provider
    burns ~3s of real `asyncio.sleep` before failing — measured as a 4.8s -> 59.4s
    regression in a single test file when this feature was switched on by
    default, and the same wasted latency on a user-facing "descubrir" click for
    any deployment that has not configured a key.

    Ollama needs no key. An unrecognised provider is attempted rather than
    silently skipped — better a wasted call than a silently disabled feature.
    """
    model = (model or "").strip().lower()
    if model.startswith(("ollama/", "ollama_chat/")):
        return True
    if model.startswith("groq/"):
        return bool(settings.GROQ_API_KEY)
    if model.startswith("gemini/"):
        return bool(settings.GEMINI_API_KEY)
    if model.startswith(("openai/", "gpt-")):
        return bool(settings.OPENAI_API_KEY)
    return True


def _is_unusable_provider(llm: object) -> bool:
    """True for a provider that cannot answer this prompt meaningfully.

    Currently the fake/deterministic adapter (`LLM_PROVIDER=fake`, and every
    test that injects it): its scripted replies follow the chat tool-call
    contract, not this node's JSON one, so a call can only end in the
    deterministic fallback anyway. Detected by duck-typing on the class name so
    this module does not import the fake adapter into production paths.
    """
    return type(llm).__name__ == "FakeLLMPort"


def _has_real_credentials(model: str) -> bool:
    """Same provider-prefix logic as `_model_credentials_ready`, WITHOUT the
    "ollama needs no key" allowance — used only to scan FALLBACK chain
    entries (round-3 fix). `LLM_FALLBACK_MODEL`/`LLM_LOCAL_MODEL` both default
    to an `ollama/...` address that may not have anything running behind it;
    treating that default as "reachable" would make the whole-chain guard
    below always return True, defeating its purpose and reintroducing the
    wasted tenacity retry/backoff latency it exists to avoid.
    """
    model = (model or "").strip().lower()
    if model.startswith("groq/"):
        return bool(settings.GROQ_API_KEY)
    if model.startswith("gemini/"):
        return bool(settings.GEMINI_API_KEY)
    if model.startswith(("openai/", "gpt-")):
        return bool(settings.OPENAI_API_KEY)
    if model.startswith(("ollama/", "ollama_chat/")):
        return False
    return True


def _expansion_credentials_ready(model: str) -> bool:
    """Can ANY model in the fallback chain the adapter would actually try
    answer this call? (round-3 fix for a confirmed WARNING).

    The old guard checked ONLY `model` (the primary) via
    `_model_credentials_ready`, ignoring `LiteLLMAdapter._get_model_chain`'s
    real fallback order (primary -> LLM_FALLBACK_MODEL ->
    LLM_LOCAL_MODEL/LLM_PRODUCTION_FALLBACK_MODEL). A deployment whose primary
    key is missing but whose fallback (Gemini, OpenAI, or a genuinely
    configured Ollama) would succeed had expansion permanently OFF — silently
    defeating the round-2 decision to default
    `EVIDENCE_QUERY_EXPANSION_ENABLED=True`. The primary keeps
    `_model_credentials_ready`'s original semantics (an explicitly configured
    ollama PRIMARY is a deliberate dev choice and needs no key); fallback
    entries are checked with `_has_real_credentials`, which does NOT treat an
    unconfigured default ollama fallback as reachable.
    """
    if _model_credentials_ready(model):
        return True
    from app.adapters.llm.litellm_adapter import LiteLLMAdapter

    chain = LiteLLMAdapter()._get_model_chain(model)  # noqa: SLF001 — same pattern as api/v1/health.py
    return any(_has_real_credentials(m) for m in chain[1:])


def _merge_unique(phrases: list[str], extra: list[str]) -> list[str]:
    """Dedupe-preserving-order merge, capped at `_MAX_PHRASES_PER_OBLIGACION`."""
    seen: set[str] = set()
    out: list[str] = []
    for term in [*phrases, *extra]:
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out[:_MAX_PHRASES_PER_OBLIGACION]


def _context_usuario_phrases(contexto_usuario: str | None) -> list[str]:
    """2-4 search phrases derived from the contratista's own monthly summary
    ("¿Qué hiciste este mes?") — evidencias/discovery-fix WU7b. Unconditional:
    always added regardless of what the LLM itself returns."""
    if not contexto_usuario or not contexto_usuario.strip():
        return []
    return _extract_keywords(contexto_usuario)[:4]


def _parse_expansion_response(raw: str, valid_ids: set[str]) -> dict[str, list[str]] | None:
    """Parse `{"<id>": ["frase", ...], ...}`. Returns None on ANY parse/shape
    failure so the caller falls back to deterministic terms entirely — a
    partially-malformed response is not trusted."""
    match = _JSON_RE.search(raw or "")
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None

    result: dict[str, list[str]] = {}
    for ob_id, phrases in data.items():
        if ob_id not in valid_ids or not isinstance(phrases, list):
            continue
        cleaned = [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
        if cleaned:
            result[ob_id] = cleaned[:_MAX_PHRASES_PER_OBLIGACION]
    return result or None


async def expand_search_terms(
    contexto: dict | None,
    obligaciones: list[dict],
    llm,
    contexto_usuario: str | None = None,
) -> dict[str, list[str]]:
    """Return `{obligacion_id: [search phrase, ...]}` — one LLM call total.

    `llm=None` (no provider available) skips the call entirely and returns
    the deterministic fallback. Any obligación the LLM's response doesn't
    cover (missing key, or the whole response is unparseable) still gets its
    deterministic fallback terms — this never returns an empty phrase list
    for an obligación that has a description.

    `contexto_usuario` (evidencias/discovery-fix WU7b) — the contratista's
    own "¿Qué hiciste este mes?" summary — feeds the prompt as the PRIMARY
    hint, and 2-4 phrases derived from it are ALWAYS merged into every
    obligación's result, whether or not the LLM itself used them. Fallback:
    empty/None `contexto_usuario` leaves behavior exactly as before.
    """
    if not obligaciones:
        return {}

    context_phrases = _context_usuario_phrases(contexto_usuario)

    fallback = _deterministic_terms(obligaciones)
    if context_phrases:
        fallback = {ob_id: _merge_unique(phrases, context_phrases) for ob_id, phrases in fallback.items()}

    # No provider, or a provider that cannot produce a real expansion: return
    # the deterministic terms without spending a round trip. This matters now
    # that EVIDENCE_QUERY_EXPANSION_ENABLED defaults to True — ~30 existing
    # tests exercise `_gather_email_evidence` with no LLM mock, and the fake
    # provider is scripted for chat turns, not for this JSON contract, so
    # calling it would only produce an unparseable answer and this same
    # fallback, one wasted call later. `settings.LLM_PROVIDER == "fake"` is
    # checked explicitly (round-3), defense-in-depth alongside the
    # `_is_unusable_provider` duck-type check.
    if llm is None or _is_unusable_provider(llm) or settings.LLM_PROVIDER == "fake":
        return fallback

    # Round-3 fix (confirmed WARNING): evaluate credentials over the WHOLE
    # fallback chain, not just the primary model — and log the skip so an
    # operator can tell "expansion ran" from "expansion silently opted out".
    if not _expansion_credentials_ready(settings.LLM_EVIDENCE_CLASSIFIER_MODEL):
        logger.info(
            "query_expansion_skipped",
            reason="no_credentials_in_chain",
            model=settings.LLM_EVIDENCE_CLASSIFIER_MODEL,
            n_obligaciones=len(obligaciones),
        )
        return fallback

    header = contract_header(contexto)
    prompt = build_query_expansion_prompt(header, obligaciones, contexto_usuario)
    valid_ids = set(fallback.keys())

    # Hard time bound: even with valid credentials a hung or throttled provider
    # must never stall the user-facing discovery click. On timeout we keep the
    # deterministic terms, exactly as on any other failure.
    try:
        resp = await asyncio.wait_for(
            llm.complete(
                [
                    LLMMessage(role="system", content=QUERY_EXPANSION_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=prompt),
                ],
                temperature=0.3,
                max_tokens=1200,
                reasoning_effort="low",
            ),
            timeout=settings.EVIDENCE_QUERY_EXPANSION_TIMEOUT_SECONDS,
        )
        parsed = _parse_expansion_response(resp.content, valid_ids)
    except TimeoutError:
        logger.warning("query_expansion_timeout", n_obligaciones=len(obligaciones))
        return fallback
    except Exception as exc:
        logger.warning("query_expansion_llm_failed", error=str(exc), n_obligaciones=len(obligaciones))
        return fallback

    if parsed is None:
        return fallback

    # Merge: any obligación the LLM didn't answer for keeps its deterministic
    # fallback instead of being silently left with zero search phrases.
    # contexto_usuario-derived phrases are merged into EVERY obligación
    # unconditionally, whether or not the LLM's own answer mentioned them.
    merged = dict(fallback)
    for ob_id, phrases in parsed.items():
        merged[ob_id] = _merge_unique(phrases, context_phrases) if context_phrases else phrases
    return merged
