# Análisis de usabilidad — creación de cuenta de cobro completa (2026-07-08)

**Pregunta de producto:** ¿la app logra crear una cuenta de cobro COMPLETA con la menor
información posible, usando Gmail/Drive/Calendar para las justificaciones y el checklist
con asistencia máxima?

**Veredicto medido: SÍ, con los quick wins de este ciclo.** El test e2e
`tests/journey/test_full_radicacion_journey.py` recorre el flujo completo por la API real
(SECOP mockeado, Google mockeado, LLM mockeado) y arroja:

| Métrica | Resultado |
|---|---|
| Inputs manuales | **6** (cédula, mes, año, modo de checklist, 1 cumplido manual estructural, clic en Radicar) |
| Pasos auto-resueltos | **14** (SECOP completa el contrato, valor de la cuenta desde `valor_mensual`, checklist estándar sembrado, 6 requisitos auto-detectados por SECOP, actividades+justificación por LLM, evidencias Gmail/Drive/Calendar descubiertas Y persistidas, ambos informes DOCX autogenerados) |

El test es además guardia de regresión: si un cambio futuro agrega un paso manual, el
assert `manual <= 6` falla mostrando el ledger itemizado.

---

## Hallazgos rankeados

### 1. El descubrimiento de evidencias no persistía nada — CORREGIDO ✅
`POST /integraciones/evidencias/descubrir` generaba justificación + links por obligación
pero nunca creaba `Actividad` ni `Evidencia` (sin commit). El usuario debía copiar el texto
a mano y la cobertura quedaba SIN_EVIDENCIA (rojo) para siempre.
**Fix:** migración 022 (evidencias tipo link: `fuente`/`url`, storage opcional) + endpoint
`POST /cuentas-cobro/{id}/evidencias/persistir` idempotente que no pisa justificaciones
escritas por el usuario + "Guardar todo" en `evidencias-tab.tsx`. La cobertura pasa a verde.

### 2. Los links de evidencia del frontend estaban rotos desde siempre — CORREGIDO ✅
`lib/evidencias-api.ts` tenía tipos que nunca coincidieron con la respuesta real del backend
(`fuente/url/relevancia` vs `source/link`): los badges de fuente y el botón "Abrir" no
funcionaban. Drift silencioso pre-existente, detectado al cablear el persistir.

### 3. No existía acción "Radicar" — CORREGIDO ✅
El checklist decía "Listo para radicar" y ahí moría el flujo: ningún botón ni endpoint
pasaba la cuenta de borrador a enviada. **Fix:** `POST /cuentas-cobro/{id}/radicar`
(valida `radicacion_lista`, reutiliza la máquina de estados, habilita re-presentar desde
RECHAZADA) + botón en el ResumenCard.

### 4. El valor de la cuenta se tipeaba siempre — CORREGIDO ✅
`contrato.valor_mensual` existía pero nunca se usaba como default. **Fix:** `valor`
opcional en el schema; el servidor lo resuelve desde el contrato.

### 5. Errores detectados por regex sobre el mensaje — CORREGIDO ✅
El frontend inferían "faltan actividades" con `/actividad/i` y "Google no conectado" por
substring — cualquier cambio de copy rompía la recuperación en silencio. **Fix:** envelope
de error con `code` (`ACTIVIDADES_MISSING`, `GOOGLE_NOT_CONNECTED`, `CHECKLIST_INCOMPLETE`);
el frontend decide por código con el heurístico viejo como fallback.

### 6. El import SECOP no trae obligaciones — PENDIENTE ⚠️
`_mapear_a_contrato_create` siempre setea `obligaciones=[]` (SECOP no tiene ese dato). Un
contrato importado "se ve listo" pero bloquea silenciosamente actividades e informes hasta
que el usuario registre obligaciones aparte (o suba el PDF del contrato para extraerlas).
**Propuesta:** al importar de SECOP, ofrecer inmediatamente subir el PDF o extraer
obligaciones de los documentos SECOP sincronizados. Encaja en el paso 1 del wizard (W3).

### 7. Tres flujos fragmentados + "Generar con IA" duplicado — ROADMAP 🗺️
Gate de checklist, wizard Generar (3 pasos) y chat `/agent` no se referencian entre sí;
`/cuentas-cobro/[id]` tiene un "Generar con IA" que golpea otro endpoint. Se resuelve con
el asistente único (ver `docs/wizard-roadmap.md`); no se consolida ahora para no
introducir regresiones.

### 8. `CUENTA_COBRO_FULL` inalcanzable — ROADMAP 🗺️
El único modo del agente que orquesta el pipeline completo (con `doc_assembly` y
`BorradorCuentaCobro`) no tiene endpoint; preview/borradores están muertos. Es el W1 del
roadmap del wizard.

### 9. La auto-detección del checklist traga errores en silencio — PENDIENTE ⚠️
El `useEffect` de radicación corre refresh-secop → auto-vincular con errores swallowed:
si SECOP falla, el usuario solo ve requisitos pendientes sin saber que hubo un intento
automático fallido. **Propuesta:** surfacear el estado del escaneo (éxito parcial/fallo)
en el ResumenCard.

### 10. El nudge de cédula no confirma el import — CORREGIDO ✅
Tras guardar la cédula (que dispara el import SECOP), no había feedback ni se invalidaba
la query del dashboard: el import corría bien pero la UI no mostraba nada (reproducido en
vivo: 9 contratos importados, tarjeta del dashboard en 0). **Fix:** el modal ahora invalida
`contratos` + `dashboard-stats` y muestra el resultado con conteo y CTA "Ver contratos".

### 11. Requisitos estructuralmente manuales — ACEPTADO (por diseño) ℹ️
`EVIDENCIAS` (cobertura por obligación, no un documento único) y los aportes propios del
usuario (seguridad social, comprobante de pago PILA, DS consecutivo, dependientes) no
tienen fuente externa posible. Son el piso irreducible de input manual. El wizard debe
pedirlos explícitamente y nada más.

### 12. `lazy="selectin"` a nivel de mapper en `CuentaCobro.actividades` / `Actividad.evidencias` — PENDIENTE ⚠️
Además del costo en round-trips (patrón ya corregido en `Usuario`), es una trampa de
corrección: cargar la entidad cachea la colección vacía en el identity map antes de un
write en la misma sesión (mordió durante B.1; workaround: ownership check por `id`).
**Propuesta:** migrar a `raise_on_sql` + `selectinload()` explícito, auditar `Contrato`.

---

## Revisión adversarial (jueces frescos, 2026-07-08)

Dos jueces independientes revisaron los diffs completos. **Must-fix encontrados y CORREGIDOS** ✅:
1. **CRÍTICO — inyección de `obligacion_id`** en `persistir_evidencias`: aceptaba obligaciones
   de otro contrato/usuario (ataque probado empíricamente). Fix: validación por set contra las
   obligaciones del contrato de la cuenta (1 query), ValidationError en cualquier mismatch.
2. **Bypass del gate de checklist**: `PATCH /estado {"estado":"enviada"}` esquivaba la validación
   de `/radicar`. Fix: el endpoint rechaza `enviada` y redirige a `POST /radicar`.
3. **XSS almacenado por URL de evidencia**: `EvidenceLink.link` aceptaba `javascript:`/`data:`.
   Fix: validador http/https en el schema.
4. **Downgrade de migración 022** reventaba con filas link. Fix: purge documentado (destructivo
   por diseño) antes de restaurar NOT NULL.

**Follow-ups aceptados (no bloqueantes):** el mount de `/mcp` en path vacío cambia el shape del
404 global (text/plain en vez de JSON) — corregir en W1; `ToolSpec.consumes_credits` es metadata
declarativa sin enforcement (TODO al cablear billing); TOCTOU en invite codes (usar
`with_for_update` si el cupo debe ser duro); logs sueltos sin gitignorear en la raíz.

**Decisión pendiente del usuario:** `MCP_ENABLED` default `True` expone `/mcp` (con auth JWT
por-tool) en Railway al deployar — confirmar o apagar por env var.

## Estado de verificación
- Suite backend completa: **916 passed, 0 failures** (baseline previo del día: 847).
- Frontend: `tsc --noEmit` limpio, `npm run build` OK (12 rutas), Playwright wizard verde.
- Journey e2e: 1 test, ledger 6 manual / 14 auto.
- MCP en runtime real: handshake `initialize` verificado contra uvicorn local.
