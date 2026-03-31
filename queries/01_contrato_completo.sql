-- ============================================================
-- Contrato completo: datos del contrato + obligaciones
--                    + cuentas de cobro + documentos cargados
-- ============================================================
-- Parámetro: :contrato_id (UUID del contrato)

-- 1. Datos del contrato
SELECT
    c.id,
    c.numero_contrato,
    c.objeto,
    c.valor_total,
    c.valor_mensual,
    c.fecha_inicio,
    c.fecha_fin,
    c.entidad,
    c.dependencia,
    c.supervisor_nombre,
    c.documento_proveedor,
    u.nombre AS contratista_nombre,
    u.cedula AS contratista_cedula,
    u.email  AS contratista_email
FROM contratos c
JOIN usuarios u ON c.usuario_id = u.id
WHERE c.id = :contrato_id
  AND c.deleted_at IS NULL;

-- 2. Obligaciones del contrato
SELECT
    o.id,
    o.tipo,
    o.orden,
    o.descripcion
FROM obligaciones o
WHERE o.contrato_id = :contrato_id
ORDER BY o.orden;

-- 3. Cuentas de cobro del contrato
SELECT
    cc.id,
    cc.mes,
    cc.anio,
    cc.estado,
    cc.valor,
    cc.fecha_envio,
    cc.pdf_storage_key,
    COUNT(a.id) AS num_actividades
FROM cuentas_cobro cc
LEFT JOIN actividades a ON a.cuenta_cobro_id = cc.id
WHERE cc.contrato_id = :contrato_id
  AND cc.deleted_at IS NULL
GROUP BY cc.id
ORDER BY cc.anio DESC, cc.mes DESC;

-- 4. Documentos cargados para el contrato (texto del contrato, instrucciones, plantillas)
SELECT
    df.id,
    df.nombre,
    df.tipo,
    df.texto_extraido IS NOT NULL AS tiene_texto,
    LENGTH(df.texto_extraido)     AS longitud_texto,
    df.created_at
FROM documentos_fuente df
WHERE df.contrato_id = :contrato_id
ORDER BY df.tipo, df.created_at;
