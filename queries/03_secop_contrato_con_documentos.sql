-- ============================================================
-- SECOP: contrato con su proceso y todos sus documentos
-- ============================================================
-- Parámetro: :cedula  (documento_proveedor del contratista)

-- 1. Contratos SECOP con su proceso asociado
SELECT
    sc.id                          AS secop_contrato_id,
    sc.id_contrato_secop,
    sc.numero_contrato,
    sc.referencia_del_contrato,
    sc.tipo_de_contrato,
    sc.estado_contrato,
    sc.nombre_entidad,
    sc.nit_entidad,
    sc.descripcion_del_proceso,
    sc.valor_del_contrato,
    sc.valor_pagado,
    sc.fecha_de_firma,
    sc.fecha_inicio,
    sc.fecha_fin,
    sc.proceso_de_compra,

    -- Proceso asociado (join por proceso_de_compra → id_proceso_secop)
    sp.id                          AS secop_proceso_id,
    sp.nombre_del_procedimiento    AS proceso_nombre,
    sp.fase                        AS proceso_fase,
    sp.estado_del_procedimiento    AS proceso_estado,
    sp.precio_base                 AS proceso_precio_base,
    sp.duracion                    AS proceso_duracion,
    sp.unidad_de_duracion

FROM secop_contratos sc
LEFT JOIN secop_procesos sp ON sc.proceso_de_compra = sp.id_proceso_secop
WHERE sc.cedula_contratista = :cedula
ORDER BY sc.fecha_de_firma DESC NULLS LAST;

-- 2. Documentos SECOP de cada contrato
SELECT
    sd.id                          AS documento_id,
    sd.id_documento_secop,
    sd.nombre_archivo,
    sd.extension,
    sd.descripcion,
    sd.fecha_carga,
    sd.url_descarga,
    sd.numero_contrato             AS numero_contrato_ref,
    sd.proceso                     AS proceso_ref,
    sd.secop_contrato_id,
    sd.secop_proceso_id,

    -- A qué contrato pertenece
    sc.referencia_del_contrato,
    sc.nombre_entidad

FROM secop_documentos sd
LEFT JOIN secop_contratos sc ON sd.secop_contrato_id = sc.id
WHERE sc.cedula_contratista = :cedula
   OR sd.secop_proceso_id IN (
       SELECT sp.id FROM secop_procesos sp
       JOIN secop_contratos sc2 ON sc2.proceso_de_compra = sp.id_proceso_secop
       WHERE sc2.cedula_contratista = :cedula
   )
ORDER BY sd.secop_contrato_id, sd.fecha_carga DESC NULLS LAST;
