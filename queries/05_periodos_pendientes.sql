-- ============================================================
-- Períodos pendientes de cobro en un contrato activo
-- Genera todos los meses en la vigencia del contrato
-- y muestra cuáles no tienen cuenta de cobro todavía.
-- ============================================================
-- Parámetros: :contrato_id, :anio_actual (ej: 2026)

WITH meses_vigencia AS (
    -- Genera serie de meses entre fecha_inicio y LEAST(fecha_fin, hoy)
    SELECT
        EXTRACT(YEAR  FROM gs)::int AS anio,
        EXTRACT(MONTH FROM gs)::int AS mes
    FROM contratos c,
         generate_series(
             DATE_TRUNC('month', c.fecha_inicio),
             DATE_TRUNC('month', LEAST(c.fecha_fin, CURRENT_DATE)),
             INTERVAL '1 month'
         ) gs
    WHERE c.id = :contrato_id
      AND c.deleted_at IS NULL
),
cuentas_existentes AS (
    SELECT mes, anio
    FROM cuentas_cobro
    WHERE contrato_id = :contrato_id
      AND deleted_at IS NULL
)
SELECT
    mv.anio,
    mv.mes,
    TO_CHAR(TO_DATE(mv.mes::text, 'MM'), 'TMMonth') AS nombre_mes,
    ce.mes IS NULL AS pendiente
FROM meses_vigencia mv
LEFT JOIN cuentas_existentes ce ON ce.mes = mv.mes AND ce.anio = mv.anio
ORDER BY mv.anio, mv.mes;
