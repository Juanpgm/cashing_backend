-- ============================================================
-- Diagrama de relaciones entre tablas (introspección)
-- Muestra todas las FK del esquema para documentar la estructura
-- ============================================================

SELECT
    tc.table_name                   AS tabla_origen,
    kcu.column_name                 AS columna_fk,
    ccu.table_name                  AS tabla_destino,
    ccu.column_name                 AS columna_pk,
    tc.constraint_name
FROM information_schema.table_constraints   tc
JOIN information_schema.key_column_usage    kcu ON tc.constraint_name  = kcu.constraint_name
JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
WHERE tc.constraint_schema = 'public'
  AND tc.constraint_type   = 'FOREIGN KEY'
ORDER BY tc.table_name, kcu.column_name;

-- ============================================================
-- Relaciones resumidas del dominio CashIn:
--
--  usuarios
--    ├─► contratos           (usuario_id)
--    │     ├─► obligaciones  (contrato_id)
--    │     ├─► cuentas_cobro (contrato_id)
--    │     │     └─► actividades      (cuenta_cobro_id)
--    │     │           ├─► evidencias (actividad_id)
--    │     │           └─► obligaciones (obligacion_id)  ← FK opcional
--    │     └─► documentos_fuente (contrato_id)  ← texto contrato, instrucciones
--    ├─► conversaciones      (usuario_id)
--    ├─► creditos            (usuario_id)
--    ├─► suscripciones       (usuario_id)
--    ├─► pagos               (usuario_id)
--    ├─► plantillas          (usuario_id, nullable = plantillas globales)
--    ├─► google_tokens       (usuario_id)
--    └─► audit_logs          (user_id)
--
--  secop_contratos
--    └─► secop_documentos    (secop_contrato_id)
--
--  secop_procesos
--    └─► secop_documentos    (secop_proceso_id)
--
--  Llaves de integración SECOP ↔ dominio:
--    contratos.documento_proveedor = secop_contratos.cedula_contratista
--    contratos.numero_contrato     = secop_contratos.numero_contrato (CO1.PCCNTR.xxx)
-- ============================================================
