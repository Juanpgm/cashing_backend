# Hoja de ruta — Asistente guiado único de cuenta de cobro

> Norte de producto: crear una cuenta de cobro COMPLETA con la menor información posible.
> El usuario solo confirma y aporta lo que ninguna fuente tiene; todo lo demás se resuelve
> con SECOP, documentos ya subidos, Gmail/Drive/Calendar y generación automática.

## Estado de partida (2026-07-08)

Hoy el flujo existe pero está fragmentado en tres rutas desconectadas:

1. **Gate de checklist** (`components/definir-checklist-gate.tsx`) — 2 pasos, una vez por cuenta.
2. **Wizard Generar** (`components/generar-tab.tsx`) — 3 pasos (Analizar → Semáforo → Generar) dentro de un tab.
3. **Chat `/agent`** — texto libre con HIL, sin modos expuestos, paralelo a todo lo anterior.

Además hay un "Generar con IA" duplicado en `/cuentas-cobro/[id]` que golpea otro endpoint
(`/agent/cuentas-cobro/{id}/generar`) sin referencia cruzada con la radicación.

El backend ya tiene casi toda la capacidad; el problema es de **orquestación y cableado**,
no de features faltantes. Los quick wins del plan actual (persistir evidencias descubiertas,
valor por defecto, radicar, códigos de error) son los prerequisitos de esta hoja de ruta.

## W1 — Orquestación backend: hacer alcanzable `CUENTA_COBRO_FULL`

**Problema:** `AgentMode.CUENTA_COBRO_FULL` (grafo supervisor: `obligations_extraction →
quality_gate → evidence_orchestrator → evidence_dedup → doc_assembly → folder_organizer →
human_review`) no es alcanzable desde ningún endpoint. `BorradorCuentaCobro` nunca se
instancia, por lo que `GET /cuentas-cobro/{id}/preview` y `/borradores` están muertos.

**Trabajo:**
- Endpoint `POST /cuentas-cobro/{id}/asistente` que arma el `AgentState` con
  `mode=CUENTA_COBRO_FULL` (patrón de inyección directa ya usado por
  `EXTRACT_OBLIGATIONS` en `document_service` y `SECOP_DISCOVERY` en `onboarding.py`).
- Streaming de progreso + pausas HIL por SSE, reutilizando la infraestructura existente de
  `/agent/sessions/{id}/stream` (`node_progress`, `hil_pause`, `completed`).
- `doc_assembly_node` persiste `BorradorCuentaCobro` para que preview/borradores funcionen.
- Contabilidad de créditos y tokens del pipeline completo (hoy solo el path de chat mide tokens).

**Criterio de salida:** un test de integración que dispara el asistente sobre una cuenta
sembrada y llega a `human_review` con borrador persistido.

## W2 — Cada paso del asistente consume el tool registry

**Dependencia:** Workstream A del plan (registry `app/tools/` + `invoke_tool`).

Los nodos del grafo supervisor y el endpoint del asistente llaman `invoke_tool(...)` en vez
de servicios directos. Consecuencia: la MISMA capacidad queda disponible para (a) el wizard
del frontend, (b) el agente interno y (c) clientes MCP externos (Claude u otros) — un cliente
MCP puede ejecutar el journey completo idéntico al de la UI.

## W3 — Frontend: stepper único `app/cuentas-cobro/[id]/asistente`

Colapsa el gate, el tab Generar, el tab Evidencias y el "Generar con IA" duplicado en un
solo flujo con riel de progreso:

| Paso | Fuente automática | Input del usuario |
|---|---|---|
| 1. Contrato | SECOP por cédula (import + sync docs) o PDF con extracción | confirmar / subir PDF si SECOP no lo tiene |
| 2. Cuenta | mes/año siguiente + `valor_mensual` del contrato | confirmar |
| 3. Checklist | estándar o inferido de pliego; SECOP auto-link + auto-vincular docs + dos niveles | elegir modo; subir SOLO lo sin fuente (seg. social, comprobantes) |
| 4. Actividades | evidencias IA (Gmail/Drive/Calendar) persistidas + `/cruzar` + generación desde obligaciones | aprobar/editar justificaciones (HIL) |
| 5. Informes | autogen DOCX actividades + supervisión | descarga/confirmación |
| 6. Radicar | validación `radicacion_lista` | un clic |

Reglas de UX que corrigen los hallazgos del análisis:
- Errores por `err.code` estructurado (nunca regex sobre el mensaje).
- Feedback explícito post-import de cédula/SECOP (hoy el nudge no confirma nada).
- Los errores de auto-detección dejan de tragarse en silencio: el riel muestra qué paso
  automático falló y ofrece el fallback manual.
- El chat `/agent` queda como entrada alternativa que deep-linkea al mismo asistente.

## W4 (opcional) — Function-calling

Pasar `ToolSpec.input_model.model_json_schema()` como `tools=` a `litellm.acompletion`
para que el router del agente seleccione tools dinámicamente. El asistente se vuelve
conversacional de punta a punta; el stepper queda como vista estructurada del mismo estado.
No bloquea nada de W1-W3.

## Secuencia y dependencias

```
Quick wins (B) ──> W1 (CUENTA_COBRO_FULL alcanzable)
Tool registry (A) ─> W2 (nodos consumen invoke_tool) ─> W3 (stepper) ─> W4 (function-calling)
```

W1 puede arrancar sin el registry (llamando servicios directo) y migrar a `invoke_tool`
cuando A aterrice; W3 requiere W1 (necesita el endpoint del asistente y SSE).
