# Design: billing-resilience-templates

## Technical Approach

Extend the LIVE radicación path (never the dead graph nodes) with two resilience
gates and a domain-model enrichment, all surfaced as `app/tools/` capabilities.
The coherence validator and the packager wrap `cuenta_cobro_service.radicar_cuenta`
(L716-747) and `informe_service.generar_zip_evidencias` (L366-460) respectively.
Model enrichment (cuota position, Adición events, per-organism templates) lands as
additive migrations `025/026/027`. Every layer follows `model → schema → service →
api → test` under strict TDD (`uv run python -m pytest`); services raise domain
exceptions, use `Decimal` for money, `AsyncSession`, `structlog`, and the
`StoragePort`/`LLMPort` ports. Slice order matches proposal §6.

## Architecture Decisions

### D1 — Coherence rule engine shape (answers §9.1)

**Choice**: A `services/coherence_validator_service.py` with a list-registry of pure
rule callables `CoherenceRule = Callable[[ValidationContext], list[Finding]]`.
`ValidationContext` bundles `(cuenta, prior_cuenta, contrato, obligaciones,
actividades, adiciones)` loaded once. Each `Finding` carries
`rule_id, severity(HARD|SOFT), codigo, mensaje, contexto: dict`.

| Rule | Detects | Severity |
|------|---------|----------|
| `stale_cuota_numero` | internal "Cuota Número" text ≠ stored `numero_cuota` | HARD |
| `copied_accumulated_value` | `valor`/seg-social block byte-equal to prior cuota | HARD |
| `pila_match` | PILA planilla number in planilla doc ≠ comprobante, same cuenta | HARD |
| `obligacion_text_mapping` | letter/index map diverges from `etiqueta` (8→7 shift) | HARD |
| `stale_clause_after_adicion` | Adición added RPC/CDP but clause text stale | HARD |
| `stale_month_in_filename` | filename month token ≠ `cuenta.mes` | SOFT |

**Alternatives considered**: class hierarchy per rule; single monolithic function.
**Rationale**: flat callable registry mirrors the existing `_GENERADORES`/`TOOL_REGISTRY`
seams, keeps each rule unit-testable in isolation, and lets severity policy live in
one place — any HARD ⇒ raise `ValidationError(code=COHERENCE_CHECK_FAILED)`; SOFT-only
⇒ return findings folded into the packager PENDIENTE list. Mapping is by normalized
TEXT everywhere (locked decision §3), reusing `text_match.similar/solo_digitos`.

### D2 — Secret scan detector (answers §9.2)

**Choice**: Reuse the **detect-secrets** library (already a pre-commit dep) as a
Python API inside `services/secret_scan_service.py`. Run its `SecretsCollection` /
`scan_line` plugin set over text-extracted member bytes; add ONE explicit
Postgres/Neon URL regex (`postgres(ql)?://[^:]+:[^@]+@`) as a belt-and-suspenders
plugin for the real leak class. Binary members are text-extracted via existing
`document_service.extraer_texto_documento` before scanning.
**Alternatives considered**: bespoke regex scanner over package bytes.
**Rationale**: detect-secrets ships maintained plugins (high-entropy, AWS, JWT,
keyword, basic-auth URL) that a bespoke scanner would re-implement with worse
false-negative risk on exactly the credential class we leaked. Hard fail-closed:
any hallazgo ⇒ `ValidationError(code=SECRET_DETECTED_IN_PACKAGE)`, no zip emitted.

### D3 — Cuota position derivation + backfill (answers §9.3)

**Choice**: Store on `CuentaCobro`: `numero_cuota: int|null`, `posicion:
enum(primera|recurrente|final)`, `informe_final: bool default False`. Derive
`numero_cuota` at creation = count of prior cuotas by `(anio,mes)` order + 1, then
persist. `posicion=primera` when `numero_cuota==1`; `final` is set EXPLICITLY (never
inferred); else `recurrente`. Migration `025` backfills `numero_cuota` via a window
over existing rows per contrato and sets `posicion` primera/recurrente only.
**Alternatives considered**: derive from a new `Contrato.numero_cuotas`; keep
inferring at read time.
**Rationale**: `numero_cuotas` does not exist and prórroga mutates it — adding it is
out of scope. Read-time inference IS the current bug (`_is_first_cuenta` L266-278),
which we replace with `posicion == PRIMERA`. Final is explicit per locked decision §3.
`CUOTA_POSITION_CONFLICT` guards two cuotas claiming the same `numero_cuota`.

### D4 — Contract Adición events + one-time obligation (answers §9.4)

**Choice**: New table `adiciones_contrato` (migration `026`): `id, contrato_id FK,
tipo enum(adicion|prorroga|otrosi), numero int, rpc_nuevo str|null, cdp_nuevo
str|null, valor_adicion Numeric|null, nueva_fecha_fin date|null, descripcion text,
fecha_evento date`. Same migration adds `Obligacion.una_vez: bool default False`
(one-time obligations blank after cuota 1). Keep `Contrato.valor_adicion` scalar for
back-compat; events are authoritative going forward.
**Alternatives considered**: reuse the audit/AuditMiddleware log.
**Rationale**: the audit log is an append-only request trail, not a queryable domain
entity that generation and the validator can read. Prórroga interaction: a prórroga
extends `fecha_fin`, so a SOFT finding warns when `informe_final=True` but a later
prórroga exists; `informe_final` stays a manual flag so prórroga never silently flips it.

### D5 — Template structure model + organism key (answers §9.5, §9.6)

**Choice**: New model `PlantillaOrganismo` (migration `027`): `id, usuario_id,
entidad str, tipo_documento str, formato str, estructura_json JSONB,
fuente_documento_id FK, timestamps`. `estructura_json = {columnas[], secciones[],
anexo_refs: bool, notas}`. Organism key = normalized `Contrato.entidad` (existing
`String(255)`), normalized with `text_match` (lowercase/strip accents/whitespace).
**Alternatives considered**: extend `Plantilla` (HTML→PDF cuenta-de-cobro) with a
nullable `estructura_json`; new `organismo` FK table.
**Rationale**: `Plantilla` has a single responsibility (PDF render) and a different
lifecycle — conflating pollutes its render path. `entidad` already carries
"DAGMA"/"COEMPRESAR", so no new organism table is warranted; noted as future
normalization if free-text entidad proves noisy.

### D6 — Progressive narrative + always-draft (answers §9.7)

**Choice**: Generation for cuota N reads prior cuotas with `numero_cuota < N`, using
only their generated informe SUMMARIES bounded by the existing `_MAX_TEXT_CHARS`
(14000); when exceeded, include the most recent K=3 summaries + counts. Every
generated informe output carries `es_borrador: bool = True` and a prepended
"BORRADOR — sujeto a revisión" header line in the DOCX. One-time obligations
(`una_vez`) render blank after cuota 1.
**Rationale**: reuses the proven char-budget guard; draft label is a constant, not an
LLM decision (locked decision §3).

### D7 — Feature flags (answers §9.8)

**Choice**: Two `core/config.py` Settings booleans — `COHERENCE_GATE_ENABLED: bool =
True`, `SECRET_SCAN_GATE_ENABLED: bool = True` — mirroring the existing
`WAITLIST_ENABLED` pattern. `radicar_cuenta` skips the validator when its flag is off;
the packager skips scanning when its flag is off, but the secret gate defaults ON and
disabling it is documented as emergency-only.

## Data Flow

    radicar_cuenta ─▶ [flag?] ─▶ coherence_validator.validar ─▶ any HARD? ─┐
         │                            │ SOFT findings                       │yes
         │                            ▼                                     ▼
         └─▶ checklist gate ─▶ cambiar_estado(ENVIADA)        raise COHERENCE_CHECK_FAILED

    generar_paquete ─▶ StoragePort.download (N seq) ─▶ per-organism numbered folders
         │                                                    │
         ▼                                                    ▼
    secret_scan.escanear ─▶ hallazgo? ─yes▶ SECRET_DETECTED_IN_PACKAGE
         │ no                                                 │
         ▼                                                    ▼
    LISTO/PENDIENTE split ─▶ pendiente? ─yes▶ PACKAGE_PENDIENTE   else ▶ zip bytes

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `app/core/exceptions.py` | Modify | Add 4 codes: `COHERENCE_CHECK_FAILED`, `SECRET_DETECTED_IN_PACKAGE`, `PACKAGE_PENDIENTE`, `CUOTA_POSITION_CONFLICT` |
| `app/services/coherence_validator_service.py` | Create | Rule registry + `ValidationContext` + `Finding` + `validar_coherencia` |
| `app/services/secret_scan_service.py` | Create | detect-secrets wrapper `escanear_paquete` + DB-URL plugin |
| `app/services/informe_service.py` | Modify | `generar_zip_evidencias`: real bytes, numbered folders, scan, LISTO/PENDIENTE; generators gain `organismo/posicion/prior_context` params |
| `app/services/cuenta_cobro_service.py` | Modify | Gate `radicar_cuenta` on validator (flag-guarded); derive/persist `numero_cuota/posicion` on create |
| `app/services/checklist_service.py` | Modify | Replace `_is_first_cuenta` with `posicion==PRIMERA`; honor `una_vez` in `_CATALOGO_SEED`; fix `listar_arbol_evidencias` text-mapping |
| `app/services/requisito_inference_service.py` | Modify | Extract template STRUCTURE (P2a); reuse CONTRATO vision fallback; degrade to flat list |
| `app/models/cuenta_cobro.py` | Modify | `numero_cuota`, `posicion`, `informe_final` |
| `app/models/obligacion.py` | Modify | `una_vez` bool |
| `app/models/adicion_contrato.py` | Create | Adición event model |
| `app/models/plantilla_organismo.py` | Create | Per-organism template structure |
| `app/schemas/coherence.py`, `schemas/paquete.py`, `schemas/adicion.py`, `schemas/plantilla_organismo.py` | Create | Pydantic I/O models |
| `app/tools/catalog/coherence.py`, `paquete.py`, `adiciones.py`, `plantillas_organismo.py` | Create | Tool wrappers (auto-registered) |
| `alembic/versions/025_*`, `026_*`, `027_*` | Create | Explicit `op.create_table`/`op.add_column` (create_all no-ops column adds — Neon-verified) |
| `app/core/config.py` | Modify | 2 feature-flag settings |
| `tests/**` | Create | Per layer (see below) |

## Interfaces / Contracts

```python
# services/coherence_validator_service.py
class Severity(StrEnum): HARD = "hard"; SOFT = "soft"

@dataclass(frozen=True)
class Finding:
    rule_id: str; severity: Severity; codigo: str; mensaje: str; contexto: dict

CoherenceRule = Callable[["ValidationContext"], list[Finding]]
RULES: list[CoherenceRule] = [...]  # registry

async def validar_coherencia(db: AsyncSession, usuario_id: uuid.UUID,
                             cuenta_id: uuid.UUID) -> list[Finding]:
    """Return all findings. Caller raises COHERENCE_CHECK_FAILED on any HARD."""

# services/secret_scan_service.py
@dataclass(frozen=True)
class SecretHallazgo: archivo: str; tipo: str; linea: int

async def escanear_paquete(miembros: list[tuple[str, bytes]]) -> list[SecretHallazgo]:
    ...

# services/informe_service.py (packager, hardened — same signature)
async def generar_zip_evidencias(db, usuario_id, cuenta_id) -> tuple[bytes, str]: ...
```

### Tool definitions

| Tool name | Params | R/W | Wraps |
|-----------|--------|-----|-------|
| `validar_coherencia_cuenta` | `cuenta_id` | read | `coherence_validator_service` |
| `generar_paquete_evidencias` | `cuenta_id` | write | `generar_zip_evidencias` |
| `registrar_adicion_contrato` | `contrato_id, tipo, numero, rpc_nuevo?, cdp_nuevo?, valor_adicion?, nueva_fecha_fin?` | write | Adición service |
| `ingerir_plantilla_organismo` | `contrato_id, documento_fuente_id` | write | template ingestion |
| `preparar_radicacion` | `cuenta_id` | write | P2c orchestration (checklist→coherence→packager) |

## Testing Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit (service) | Each coherence rule (HARD/SOFT), incl. 8→7 obligación-shift regression | Factory-boy fixtures, no LLM; assert `Finding` list |
| Unit (service) | Secret scan catches real-leak corpus (Neon URL, API keys); clean pass | Byte corpus from the shipped leak; `moto[s3]` for bytes |
| Unit (service) | `numero_cuota` derivation + backfill idempotence; Adición events; template struct extract (LLM mocked, degrade path) | aiosqlite |
| Integration (API/tool) | `radicar` blocked on HARD finding + `COHERENCE_CHECK_FAILED`; packager `SECRET_DETECTED_IN_PACKAGE`/`PACKAGE_PENDIENTE`; flags off = bypass | `httpx.AsyncClient` + `invoke_tool` |
| Contract | Update `test_radicar._CODIGOS_OBLIGATORIOS`; keep JourneyLedger green with new gate steps | Deliberate ledger update |

## Migration / Rollout

- `025` `CuentaCobro.numero_cuota/posicion/informe_final` (+ backfill window); `026`
  `adiciones_contrato` table + `Obligacion.una_vez`; `027` `plantilla_organismo` table.
  All additive (nullable / new tables). Column adds via explicit `op.add_column`
  (SQLAlchemy `create_all` no-ops column additions — must be applied on Neon).
- Rollback: `alembic downgrade -1` per slice then revert code; tools de-register on
  module removal. Gates are flag-guarded (`COHERENCE_GATE_ENABLED`,
  `SECRET_SCAN_GATE_ENABLED`) for emergency disable.
- Sequence document-touching slices (#5/#7) AFTER `backend-local-first-sync` merges;
  second-merger rebases migration numbering.

## Open Questions

- [ ] None blocking. Free-text `entidad` normalization robustness across organisms is
      a monitored risk (D5) — revisit only if match noise appears in practice.

## Clarification: PACKAGE_PENDIENTE vs CHECKLIST_INCOMPLETE (added slice #3, task 3.0b)

Carried over from the `cuota-packager` spec (slice #2 verify-report WARNING 2) so both
documents agree: `PACKAGE_PENDIENTE` is an **obligación-level packaging completeness**
signal owned by `informe_service.generar_zip_evidencias(modo="final")` (LISTO/PENDIENTE
per obligación, from D2's data flow). `CHECKLIST_INCOMPLETE` is a separate
**requisito/checklist completeness** signal owned by `checklist_service` +
`cuenta_cobro_service.radicar_cuenta`. Slice #7's `preparar_radicacion` orchestrator
(File Changes, `radicacion_prep_service.py`) calls checklist → coherence → packager in
sequence and surfaces whichever gate fails first — it never merges the two signals into
one "readiness" flag.
