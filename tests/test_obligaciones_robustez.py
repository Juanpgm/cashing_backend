"""Robustez del extractor verbatim frente a texto de PDF degradado.

Cubre los tres síntomas reportados en producción:

1. Sobre-inclusión: no acota la sección y mete obligaciones generales.
2. Enumeración aplanada: el PDF perdió los saltos de línea y los marcadores
   quedaron inline, devolviendo un único bloque comprimido en vez de una lista.
3. Sección equivocada: el término aparece primero en una frase suelta y el
   extractor agarra fragmentos de otra parte del contrato.

El objetivo es PREDICTIBILIDAD: el extractor entrega una lista limpia o
devuelve ``[]`` para que el LLM tome el relevo — nunca un resultado a medias.
"""

from __future__ import annotations

from app.agent.tools.contract_parser import extract_obligaciones_verbatim

# ── Síntoma 2: enumeración aplanada por la extracción del PDF ───────────────


def test_flattened_numeric_enumeration_is_split() -> None:
    """Marcadores numéricos inline (sin saltos de línea) se separan igual."""
    texto = (
        "CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA: "
        "1. Diseñar e implementar los módulos del sistema de información. "
        "2. Realizar las pruebas unitarias y de integración de los componentes. "
        "3. Capacitar a los funcionarios en el uso del nuevo sistema. "
        "4. Las demás actividades que le asigne la Secretaría y que se relacionen "
        "con el objeto del contrato. "
        "CLÁUSULA TERCERA — VALOR DEL CONTRATO: treinta millones de pesos."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 4
    assert [o.etiqueta for o in result] == ["1", "2", "3", "4"]
    assert result[0].descripcion.startswith("Diseñar e implementar")
    assert "Las demás actividades" in result[-1].descripcion
    # Verbatim: cada descripción sigue siendo substring textual del contrato.
    for ob in result:
        assert ob.descripcion in texto


def test_flattened_does_not_break_on_legal_references() -> None:
    """'Ley 80.' o cifras NO deben confundirse con marcadores de lista."""
    texto = (
        "OBLIGACIONES ESPECÍFICAS: conforme a la Ley 80 de 1993. "
        "1. Prestar asesoría jurídica en los procesos de contratación estatal. "
        "2. Las demás que le asigne el supervisor relacionadas con el objeto."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 2
    assert result[0].descripcion.startswith("Prestar asesoría jurídica")
    assert "Las demás" in result[-1].descripcion


def test_flattened_alpha_enumeration_is_split() -> None:
    """Marcadores alfabéticos inline (a) b) c)) también se separan."""
    texto = (
        "ACTIVIDADES ESPECÍFICAS: "
        "a) Realizar el diagnóstico territorial del municipio. "
        "b) Elaborar los estudios previos de los proyectos de inversión. "
        "c) Las demás que asigne la Secretaría relacionadas con el objeto."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 3
    assert [o.etiqueta for o in result] == ["a", "b", "c"]
    assert result[0].descripcion.startswith("Realizar el diagnóstico")


def test_flattened_uppercase_alpha_enumeration_is_split() -> None:
    """Marcadores en MAYÚSCULA inline (A) B) C)) — patrón típico de SECOP II.

    Reproduce el fallo real del EJEMPLO #1: los ítems venían aplanados con
    letras mayúsculas, y el extractor solo manejaba minúsculas y números.
    """
    texto = (
        "OBLIGACIONES ESPECÍFICAS: A) Ejecutar los procesos misionales de la "
        "entidad en el marco del ciclo PHVA B) Servir de enlace entre la "
        "Secretaría y el despacho del alcalde C) Apoyar la generación de "
        "información requerida por el despacho D) Las demás actividades que le "
        "asigne la Secretaría que se relacionen con el objeto del contrato. "
        "OBLIGACIONES GENERALES DEL CONTRATISTA: A) Pagar la seguridad social."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 4
    assert [o.etiqueta for o in result] == ["A", "B", "C", "D"]
    assert result[0].descripcion.startswith("Ejecutar los procesos misionales")
    # El cierre corta antes de las OBLIGACIONES GENERALES.
    joined = " ".join(o.descripcion for o in result).lower()
    assert "seguridad social" not in joined


def test_catch_all_not_polluted_by_inline_heading() -> None:
    """El cierre no debe arrastrar la cláusula siguiente pegada en la misma línea.

    Reproduce el EJEMPLO #1: el PDF aplana "…del servicio. PARÁGRAFO I: …" en una
    sola línea; el cierre debe terminar limpio, sin el PARÁGRAFO.
    """
    texto = (
        "OBLIGACIONES ESPECÍFICAS: A) Ejecutar los procesos misionales de la "
        "entidad B) Las demás actividades que le asigne la Secretaría que se "
        "relacionen con el objeto del contrato. PARÁGRAFO I: El contratista "
        "deberá soportar con evidencias el cumplimiento de sus actividades."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 2
    assert result[-1].descripcion.lower().endswith("del contrato")
    joined = " ".join(o.descripcion for o in result).lower()
    assert "parágrafo" not in joined
    assert "evidencias" not in joined


def test_prefers_specific_list_over_preceding_general_list() -> None:
    """Una lista GENERAL que precede a la específica no debe ganar.

    Reproduce el EJEMPLO #2: primero viene "Obligaciones del contratista" con
    deberes generales (seguridad social), y después "Actividades del Contrato"
    con las específicas que cierran en "Las demás…". Debe elegirse la segunda.
    """
    texto = (
        "CUARTA. Obligaciones del contratista. EL CONTRATISTA tendrá: "
        "a) Cumplir las normas internas de la entidad. "
        "b) Cumplir con los aportes al Sistema de Seguridad Social Integral. "
        "QUINTA. Supervisión. La supervisión estará a cargo del jefe de área. "
        "SEXTA. Actividades del Contrato. Estarán a cargo del contratista: "
        "1. Ejecutar acciones profesionales de restauración ecológica. "
        "2. Desarrollar acciones de control de factores de deterioro ambiental. "
        "3. Las demás actividades que le asigne la supervisión relacionadas con "
        "el objeto del contrato. "
        "SEPTIMA. Exclusividad. Las partes convienen que no hay exclusividad."
    )
    result = extract_obligaciones_verbatim(texto)
    assert [o.etiqueta for o in result] == ["1", "2", "3"]
    joined = " ".join(o.descripcion for o in result).lower()
    assert "seguridad social" not in joined
    assert result[0].descripcion.startswith("Ejecutar acciones profesionales")


# ── Síntoma 1: sobre-inclusión de obligaciones generales ───────────────────


def test_generales_after_catch_all_are_excluded() -> None:
    """Lo que viene DESPUÉS del 'Las demás…' es general y no se incluye."""
    texto = (
        "OBLIGACIONES DEL CONTRATISTA:\n"
        "1. Prestar asesoría jurídica en los procesos de selección abreviada.\n"
        "2. Las demás que le asigne el supervisor y se relacionen con el objeto.\n"
        "3. Pagar los aportes al sistema de seguridad social integral.\n"
        "4. Mantener vigente la afiliación a riesgos laborales (ARL).\n"
        "PLAZO DE EJECUCIÓN: seis meses."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 2
    joined = " ".join(o.descripcion for o in result).lower()
    assert "seguridad social" not in joined
    assert "riesgos laborales" not in joined


# ── Síntoma 3: sección equivocada / fragmentos de otra parte ────────────────


def test_skips_prose_mention_and_finds_real_section() -> None:
    """Una mención en prosa no debe ganarle a la sección real con lista."""
    texto = (
        "CLÁUSULA PRIMERA — OBJETO: definir las obligaciones específicas del "
        "contratista conforme al estudio previo y la propuesta presentada.\n"
        "CLÁUSULA SEGUNDA — OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA:\n"
        "1. Elaborar el diagnóstico técnico del proyecto de infraestructura.\n"
        "2. Las demás que se le asignen relacionadas con el objeto del contrato.\n"
        "VALOR DEL CONTRATO: diez millones de pesos."
    )
    result = extract_obligaciones_verbatim(texto)
    assert len(result) == 2
    assert result[0].descripcion.startswith("Elaborar el diagnóstico")


def test_prose_only_section_yields_empty_not_garbage() -> None:
    """Sección sin lista enumerada → [] (escala al LLM), nunca un bloque de prosa."""
    texto = (
        "OBJETO DEL CONTRATO: el contratista prestará servicios profesionales "
        "de ingeniería civil para el seguimiento de obras de infraestructura "
        "en el municipio durante la vigencia del contrato.\n"
        "VALOR: veinte millones."
    )
    assert extract_obligaciones_verbatim(texto) == []


def test_oversized_single_block_is_rejected() -> None:
    """Un único 'ítem' enorme con marcadores perdidos se rechaza para escalar."""
    cuerpo = " ".join(f"obligación contractual número {n} del proyecto" for n in range(60))
    texto = f"OBLIGACIONES ESPECÍFICAS:\n- {cuerpo}\nVALOR DEL CONTRATO: diez millones."
    assert extract_obligaciones_verbatim(texto) == []
