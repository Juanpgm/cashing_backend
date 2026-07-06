"""Entity profile node — derives entity slug and stores/retrieves profile."""

from __future__ import annotations

import re
import uuid

import structlog

from app.agent.state import AgentState

logger = structlog.get_logger("agent.nodes.entity_profile")

# ── Entity type classifier ────────────────────────────────────────────────────
# Maps normalized name substrings to entity type codes used as few-shot keys.
_ENTITY_TYPE_MAP: list[tuple[str, str]] = [
    ("sena", "sena"),
    ("icbf", "icbf"),
    ("dian", "dian"),
    ("invias", "invias"),
    ("ministerio", "ministerio"),
    ("mintic", "mintic"),
    ("minsalud", "minsalud"),
    ("mineducacion", "mineducacion"),
    ("minambiente", "minambiente"),
    ("minjusticia", "minjusticia"),
    ("minhacienda", "minhacienda"),
    ("cancilleria", "cancilleria"),
    ("defensa", "defensa"),
    ("policia", "policia"),
    ("fuerzas militares", "fuerzas_militares"),
    ("ejercito", "fuerzas_militares"),
    ("armada", "fuerzas_militares"),
    ("fuerza aerea", "fuerzas_militares"),
    ("alcaldia", "alcaldia"),
    ("gobernacion", "gobernacion"),
    ("universidad", "universidad"),
    ("hospital", "hospital"),
    ("eps", "eps"),
    ("ips", "ips"),
    ("colpensiones", "colpensiones"),
    ("dane", "dane"),
    ("dnp", "dnp"),
    ("agencia", "agencia"),
    ("corporacion", "corporacion"),
    ("empresa", "empresa_estatal"),
    ("ins", "ins"),
    ("inpec", "inpec"),
    ("igac", "igac"),
]


def _detect_entity_type(name: str) -> str:
    """Classify entity type from its name for few-shot prompt selection.

    Returns a short type code like 'sena', 'alcaldia', 'ministerio', etc.
    Falls back to 'entidad_publica' when no pattern matches.
    """
    normalized = name.lower()
    # Remove accents for comparison
    normalized = re.sub(r"[áàä]", "a", normalized)
    normalized = re.sub(r"[éèë]", "e", normalized)
    normalized = re.sub(r"[íìï]", "i", normalized)
    normalized = re.sub(r"[óòö]", "o", normalized)
    normalized = re.sub(r"[úùü]", "u", normalized)
    normalized = re.sub(r"[ñ]", "n", normalized)

    for keyword, entity_type in _ENTITY_TYPE_MAP:
        if keyword in normalized:
            return entity_type

    return "entidad_publica"


def _slugify(text: str) -> str:
    """Convert entity name to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[áàä]", "a", text)
    text = re.sub(r"[éèë]", "e", text)
    text = re.sub(r"[íìï]", "i", text)
    text = re.sub(r"[óòö]", "o", text)
    text = re.sub(r"[úùü]", "u", text)
    text = re.sub(r"[ñ]", "n", text)
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text)
    return text[:64]


async def entity_profile_node(state: AgentState) -> AgentState:
    """Derive entity profile from requirements and contract data.

    Reads: entity_requirements, contrato_extraido
    Writes: entity_profile_id, current_phase
    """
    requirements: dict = state.get("entity_requirements") or {}
    contrato: dict = state.get("contrato_extraido") or {}

    # Resolve entity name from requirements first, then contract
    entidad = (
        requirements.get("entidad")
        or contrato.get("entidad")
        or contrato.get("nombre_entidad")
        or ""
    )

    if not entidad:
        return {
            **state,
            "error": "No se pudo determinar la entidad contratante",
            "current_phase": "entity_profile",
        }

    slug = _slugify(entidad)

    # Re-use existing profile_id if the slug matches the current contrato_extraido
    existing_id = state.get("entity_profile_id")
    if existing_id:
        await logger.ainfo("entity_profile_reused", slug=slug, profile_id=str(existing_id))
        return {**state, "current_phase": "entity_profile"}

    # Deterministic UUID v5 so same entity always gets same ID
    profile_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"cashin.entity.{slug}")
    entity_type = _detect_entity_type(entidad)
    await logger.ainfo(
        "entity_profile_created",
        slug=slug,
        entity_type=entity_type,
        profile_id=str(profile_id),
    )

    return {
        **state,
        "entity_profile_id": profile_id,
        "entity_type": entity_type,
        "current_phase": "entity_profile",
    }
