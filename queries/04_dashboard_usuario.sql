-- ============================================================
-- Dashboard del usuario: resumen de contratos y cuentas
-- ============================================================
-- Parámetro: :usuario_id (UUID del usuario)

SELECT
    c.id                            AS contrato_id,
    c.numero_contrato,
    c.entidad,
    c.objeto,
    c.fecha_inicio,
    c.fecha_fin,
    c.valor_total,
    c.valor_mensual,
    c.documento_proveedor,

    -- Conteo de cuentas por estado
    COUNT(cc.id)                    AS total_cuentas,
    COUNT(cc.id) FILTER (WHERE cc.estado = 'borrador')  AS cuentas_borrador,
    COUNT(cc.id) FILTER (WHERE cc.estado = 'enviada')   AS cuentas_enviadas,
    COUNT(cc.id) FILTER (WHERE cc.estado = 'aprobada')  AS cuentas_aprobadas,
    COUNT(cc.id) FILTER (WHERE cc.estado = 'pagada')    AS cuentas_pagadas,
    COUNT(cc.id) FILTER (WHERE cc.estado = 'rechazada') AS cuentas_rechazadas,

    -- Última cuenta de cobro
    MAX(cc.anio * 100 + cc.mes)     AS ultimo_periodo,

    -- Valor cobrado total (solo pagadas)
    COALESCE(SUM(cc.valor) FILTER (WHERE cc.estado = 'pagada'), 0) AS total_cobrado,

    -- Documentos configurados
    COUNT(DISTINCT df.id) FILTER (WHERE df.tipo = 'contrato'      AND df.texto_extraido IS NOT NULL) AS docs_contrato,
    COUNT(DISTINCT df.id) FILTER (WHERE df.tipo = 'instrucciones')                                    AS docs_instrucciones,

    -- Obligaciones registradas
    COUNT(DISTINCT o.id)            AS num_obligaciones,

    -- Listo para generar cuentas
    (
        COUNT(DISTINCT df.id) FILTER (WHERE df.tipo = 'contrato' AND df.texto_extraido IS NOT NULL) > 0
        AND COUNT(DISTINCT df.id) FILTER (WHERE df.tipo = 'instrucciones') > 0
        AND COUNT(DISTINCT o.id) > 0
    )                               AS configuracion_completa

FROM contratos c
LEFT JOIN cuentas_cobro  cc ON cc.contrato_id  = c.id  AND cc.deleted_at IS NULL
LEFT JOIN documentos_fuente df ON df.contrato_id = c.id AND df.usuario_id = c.usuario_id
LEFT JOIN obligaciones    o  ON o.contrato_id  = c.id
WHERE c.usuario_id = :usuario_id
  AND c.deleted_at IS NULL
GROUP BY c.id
ORDER BY c.fecha_fin DESC NULLS LAST;
