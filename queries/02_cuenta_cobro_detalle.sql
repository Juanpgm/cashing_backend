-- ============================================================
-- Cuenta de cobro completa: actividades + obligaciones + evidencias
-- ============================================================
-- Parámetro: :cuenta_cobro_id (UUID de la cuenta de cobro)

SELECT
    cc.id                          AS cuenta_id,
    cc.mes,
    cc.anio,
    cc.estado,
    cc.valor,
    cc.fecha_envio,

    -- Contrato
    c.numero_contrato,
    c.objeto,
    c.entidad,
    c.dependencia,
    c.supervisor_nombre,
    c.valor_mensual                AS valor_mensual_contrato,

    -- Contratista
    u.nombre                       AS contratista_nombre,
    u.cedula                       AS contratista_cedula,

    -- Actividad
    a.id                           AS actividad_id,
    a.descripcion                  AS actividad_descripcion,
    a.justificacion                AS actividad_justificacion,
    a.fecha_realizacion,

    -- Obligación relacionada
    o.descripcion                  AS obligacion_descripcion,
    o.tipo                         AS obligacion_tipo

FROM cuentas_cobro cc
JOIN contratos   c ON cc.contrato_id  = c.id
JOIN usuarios    u ON c.usuario_id    = u.id
LEFT JOIN actividades a ON a.cuenta_cobro_id = cc.id
LEFT JOIN obligaciones o ON a.obligacion_id  = o.id
WHERE cc.id = :cuenta_cobro_id
  AND cc.deleted_at IS NULL
ORDER BY a.fecha_realizacion NULLS LAST, a.created_at;
