# Stepper UX Specification

Retroactive spec, amended 2026-09-19 to match frontend `master` `11bf4c4` (originally written against `db45c9f`). Frontend (`cashing-frontend`). Task ids refer to `tasks.md`.

## Purpose

The radicacion wizard is a three-screen experience with one entry point, one confirmation idiom, a consistent visual system, and a mobile/a11y floor.

## Requirements

### Requirement: Three groups (3.4, 3.5, 5.3)

The wizard MUST expose 3 groups: "Preparar" (1, screen "Tu cuota"), "Completá tu cuota" (2, raw steps 3-6, revealed as a **staged** rail — see the next requirement — not all at once), and "Revisá y radicá" (3). Group 2 MUST declare `revealMode: "staged"`, `members: [4, 6, 3, 5]` and `stagedOrder: [CONTEXTO_MEMBER, 4, 6, 3, 5]`; `members` MUST stay raw-steps-only so gating derivations do not depend on display order or on the virtual Contexto section. A stale group index above 3 MUST clamp to 3. Rows in group 2 MUST show a collapsed default with one status chip and one primary action, with secondary actions behind a "Detalles" toggle; errors MUST NOT be hidden inside it.

#### Scenario: Navigation within group 2
- WHEN the user navigates to step 5 or 4 from within group 2
- THEN that step's section is scrolled into view and focused

#### Scenario: Progress
- WHEN classification runs THEN a real `procesadas/total` bar is shown
- AND when justification generation (step 6) or package generation (step 7) runs and passes the display threshold THEN a staged (non-numeric) progress indicator is shown whose `role="progressbar"` has an accessible name (its `label`, defaulting to "Progreso")

### Requirement: Screen 2 "Completá tu cuota" is a staged rail, context first, with one completeness model (5.3)

Group 2 MUST walk its sections in this display order: **Contexto** (virtual, no backend step) → **Evidencias** (raw step 4) → **Justificaciones** (6) → **Soportes** (3) → **Formatos e informes** (5). Exactly one section MUST render its full body at a time (the first not-yet-passable section, or the last one when all are passable); sections before it MUST collapse to a one-line summary with "Editar"; sections after it MUST render a locked stub naming the single next action, in this screen's vocabulary (never "checklist"). A sticky rail header MUST list all five sections with their current mark at every viewport width.

One pure model (`computeSectionModel`) MUST be the only source for the rail, the collapsed rows, the acknowledgement line, the closing receipt, the footer label and hint, and the group gate. It MUST keep two questions apart: **done** — the backend says the section is finished (the only thing allowed to claim completion) — and **passable** — the walk may move on (done, or, for Evidencias only, at least one persisted evidencia). A section that is passable but not done MUST render as `partial` with its real counts and MUST NOT be described as complete. A section the backend calls done MUST NEVER render as locked. Contexto MUST gate the rail but MUST NOT be a raw member, so it can never be the reason the wizard blocks radicación.

The checklist inside this screen MUST show only upload-type documents ("Soportes"); the two auto-generatable informes MUST live in "Formatos e informes" next to their template upload, and evidence coverage MUST live only in the Evidencias step. Formatos e informes MUST follow the backend's `StepState.complete` and `detail.informes_pendientes` (informe codes MUST be shown as plain names, never as raw `INFORME_*` codes).

#### Scenario: Context first
- GIVEN a cuenta with no contexto written and no skip chosen
- WHEN group 2 opens
- THEN Contexto is the only open section, every later section is a locked stub with one plain-language next action, and evidence discovery is not auto-fired

#### Scenario: Incomplete context blocks the rail, not radicación
- GIVEN Contexto is untouched
- THEN Evidencias and the sections after it stay locked
- AND the wizard footer does not name Contexto as its blocking reason

#### Scenario: Contexto done by text, by skip, or by nothing left to ask
- WHEN text is written, OR the user chooses "Continuar sin contexto", OR the cuenta is no longer editable, OR the cuenta query has failed
- THEN Contexto counts as done and Evidencias becomes active (no dead end)
- AND the skip is remembered per cuenta in the browser only, and is forgotten once a contexto is actually written
- AND while the cuenta query is still loading Contexto stays active

#### Scenario: Section order and unlocks
- WHEN at least one evidencia is persisted THEN Justificaciones unlocks
- AND WHEN justificaciones are done THEN Soportes (step 3) unlocks next, not Formatos e informes
- AND WHEN Soportes are done THEN Formatos e informes becomes active

#### Scenario: Partial Evidencias is never "Completo"
- GIVEN 1 of 3 obligaciones is covered (Evidencias secured, not complete)
- THEN the rail advances to Justificaciones, Evidencias shows a "Parcial" pill with its count, and no section says "Listo" or "Completo" for it

#### Scenario: Zero-obligaciones contrato
- GIVEN a contrato with no obligaciones (the backend reports steps 4 and 6 complete)
- THEN the walk advances on the backend's own completeness and never locks Justificaciones or Formatos e informes

#### Scenario: Acknowledgement of a section finished while the user works in it
- WHEN the active section becomes done or passable while the user is in it (including a contexto written after the cuenta query resolved, in either order)
- THEN its body stays mounted with a success line and a "Continuar" button, and no other section opens until the user presses it
- AND focus is not moved by the data transition, only by the user's own "Continuar"; the newly active section is announced in a polite live region
- AND no acknowledgement is shown for a section already complete on mount, for the last section (its receipt is the closing row), for work finished while the user was on another group, or for Contexto skipped by the user (the skip is its own click)

#### Scenario: Resuming mid-stage
- GIVEN the user reopens a cuenta whose backend state already has Evidencias secured
- THEN the rail opens on Justificaciones (the position is derived from backend state, not from local memory) with no acknowledgement for what was already done

#### Scenario: Closing receipt and footer
- WHEN the rail has walked to the last section, every rail section is done and every raw member is done THEN the receipt reads "Todo listo" and the footer primary is renamed to the next screen
- AND WHEN the rail is at the end but a raw member is not done THEN the receipt reads "Casi listo" with the same counts the rail shows and the footer hint reads "Para revisar y radicar, …"
- AND no receipt is shown while an acknowledgement is pending or while the rail is at an earlier section, and the footer primary keeps "Siguiente" disabled while an acknowledgement is pending
- AND the wizard MUST NOT auto-advance out of group 2

#### Scenario: Editar and locked marks
- WHEN the user presses "Editar" or a completed rail mark THEN that section opens and any other collapsed section stays collapsed (one expanded at a time)
- AND a locked rail mark is not a click target

#### Scenario: Mobile floor
- WHEN the wizard renders at phone width
- THEN the rail header still lists all five sections, the visible labels use their short forms (e.g. "Justific.", "Formatos"), and each mark keeps the full section name as visually hidden text for assistive technology

### Requirement: Screen 1 "Tu cuota" (3.3)

Screen 1 MUST show a pre-filled cuota summary (including `informe_final`), with `numero_cuota` and `fecha_transaccion` behind a default-closed "Editar" disclosure. The checklist-mode decision MUST default to the standard checklist; inference MUST be opt-in, show a per-row `nuevo`/`coincide` marker, and allow per-row undo of removals. Manually added rows MUST receive a derived codigo (max 50 chars, unique within the batch) so they are actually submitted.

#### Scenario: Named manual row
- WHEN a user adds a row with only a label and applies
- THEN the submitted payload contains that row with a derived codigo of at most 50 chars

#### Scenario: Malformed inference response
- WHEN inference returns a 200 without `requisitos`/`avisos` arrays
- THEN the UI does not crash

### Requirement: Screen 3 "Revisá y radicá" (3.5, 3.2)

Screen 3 MUST trigger package generation through the async job, resume a `running` job on mount, and poll only while the job is `running` (or `pending` after a local trigger). It MUST show a data-driven preview (filename/size, `es_borrador`, per-obligacion readiness, coherence findings) and MUST offer the three standalone downloads (actividades, supervision, evidencias) gated as in the legacy Generar tab. A trigger error MUST cause one confirmation refetch. A stalled job MUST offer retry only after the server stale window (300s) plus margin.

#### Scenario: Opening screen 3 for a never-triggered cuenta
- THEN no polling loop starts

#### Scenario: Trigger fails in transport
- WHEN the trigger call errors but the server enqueued a job
- THEN a confirmation refetch re-attaches polling

### Requirement: One wizard route, reachable from the account detail page (3.2, 3.2a, 5.4)

Creating a cuenta or opening its checklist MUST route `borrador`/`rechazada` cuentas to `/radicar/{id}`; `enviada` MUST offer reabrir-then-edit; `aprobada`/`pagada` MUST keep the legacy detail page as the historical view. The `RadicarModoModal` and the contract-detail creation modal MUST NOT exist. The account detail page `/cuentas-cobro/{id}` MUST show a primary "Completar en el wizard" link to `/radicar/{id}` only while the cuenta is `borrador` or `rechazada` (a second link to the same single route, not a second flow); "Generar con IA" MUST be a secondary action there.

#### Scenario: New cuenta from a contract
- WHEN a cuenta is created from contract detail
- THEN the user lands on `/radicar/{id}`

#### Scenario: Direct entry from the account detail page
- GIVEN the account detail page of a `borrador` or `rechazada` cuenta
- THEN "Completar en el wizard" is shown and its `href` is `/radicar/{id}`
- AND for `enviada`, `aprobada` or `pagada` the link is not rendered

### Requirement: Single confirmation idiom (3.6)

Destructive and reversible-but-significant actions MUST use `ConfirmDialog` (no `window.confirm`, no inline "Eliminar? Si/No"), including obligacion-delete and requisito-desvincular. When both an obligaciones draft and a dirty form guard navigation, the dialogs MUST appear sequentially with no navigation silently lost. Delete errors MUST render inside the dialog.

#### Scenario: Failed delete
- WHEN a delete fails
- THEN the error is visible inside the dialog and does not leak to a later dialog

### Requirement: Visual and a11y floor (3.7-3.9, 4.6)

The app MUST use `lang="es"`, label-associated inputs, keyboard-operable drop zones (nested buttons MUST NOT re-trigger the parent), one brand hue and one danger/success pair from `globals.css` tokens, a 5-variant `Button`, and a single voseo register. Below `md` the sidebar MUST collapse to a drawer, the wizard MUST be full-screen, and `main` MUST use the full viewport width. Journey-critical elements MUST expose stable `data-testid`s.

#### Scenario: Mobile 390px
- WHEN any authenticated page renders at 390px
- THEN `main` is not squeezed by the sidebar and axe reports no serious violations on scanned pages

## Open User Decision (recorded, NOT decided)

- **Group 2 height.** Under the earlier `revealMode: "all"` group 2 spanned ~5800-7100 px in a ~373 px scroll band, and the choice (collapse members by default / accordion / keep) was left to the user (3.4). The staged rail changes the context — PR #68 states that only one section renders at a time, which mitigates the length — but no artifact records a decision. This spec neither requires nor forbids any of the three options; it stays open until the user decides.

## Out of Scope (tracked follow-ups)

- "Sin clasificar" row still has two always-visible actions.
- True 3-state inference diff; "Volver" confirmation; drawer Escape/focus-trap.
- Token adoption (most raw violet/rose/emerald remain), `text-gray-400` contrast outside scanned pages, `accent` tone contrast.
- Screen-reader announcement of classification completion after removing `aria-live` (the staged rail's own live region for the newly active section is in scope above).
- Screen 3 still uses some older copy ("Ir al paso 2", "validando checklist") (5.3 follow-up).
- Contexto's `sinGuardar` flag stays `true` after a cuenta becomes non-editable mid-session (deliberate; 5.3 follow-up).
- `e2e/screens-r4.spec.ts` and `e2e/screens-r5.spec.ts` (env-gated screenshot passes) sit outside the `journey|edge-cases|smoke` layout.
