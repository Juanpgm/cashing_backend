-- ============================================================
-- Contexto completo para el agente IA al generar una cuenta de cobro
-- Devuelve todo lo necesario en un solo query
-- ============================================================
-- Parámetro: :contrato_id

SELECT
    -- Contrato
    c.id                            AS contrato_id,
    c.numero_contrato,
    c.objeto,
    c.entidad,
    c.dependencia,
    c.supervisor_nombre,
    c.fecha_inicio,
    c.fecha_fin,
    c.valor_total,
    c.valor_mensual,
    c.documento_proveedor,

    -- Contratista
    u.nombre                        AS contratista_nombre,
    u.cedula                        AS contratista_cedula,
    u.email                         AS contratista_email,

    -- Texto del contrato (para dar contexto al agente)
    df_contrato.texto_extraido      AS texto_contrato,
    df_contrato.nombre              AS archivo_contrato,

    -- Instrucciones del usuario para el agente
    df_instruc.texto_extraido       AS instrucciones_usuario,
    df_instruc.nombre               AS archivo_instrucciones,

    -- Obligaciones como JSON array
    (
        SELECT JSON_AGG(
            JSON_BUILD_OBJECT(
                'id',          o.id,
                'tipo',        o.tipo,
                'orden',       o.orden,
                'descripcion', o.descripcion
            ) ORDER BY o.orden
        )
        FROM obligaciones o
        WHERE o.contrato_id = c.id
    )                               AS obligaciones_json,

    -- Cuentas de cobro previas (para contexto de actividades pasadas)
    (
        SELECT JSON_AGG(
            JSON_BUILD_OBJECT(
                'mes',    cc.mes,
                'anio',   cc.anio,
                'estado', cc.estado,
                'valor',  cc.valor
            ) ORDER BY cc.anio DESC, cc.mes DESC
        )
        FROM cuentas_cobro cc
        WHERE cc.contrato_id = c.id
          AND cc.deleted_at IS NULL
    )                               AS cuentas_previas_json

FROM contratos c
JOIN usuarios u ON c.usuario_id = u.id
-- Documento del contrato (tipo=contrato con texto extraído)
LEFT JOIN documentos_fuente df_contrato ON
    df_contrato.contrato_id  = c.id
    AND df_contrato.tipo     = 'contrato'
    AND df_contrato.texto_extraido IS NOT NULL
-- Instrucciones del usuario (tipo=instrucciones)
LEFT JOIN documentos_fuente df_instruc ON
    df_instruc.contrato_id   = c.id
    AND df_instruc.tipo      = 'instrucciones'
WHERE c.id = :contrato_id
  AND c.deleted_at IS NULL
LIMIT 1;
