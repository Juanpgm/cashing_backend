"""Prompt for extracting specific contract obligations from a Colombian government contract — v4."""

OBLIGACIONES_SYSTEM = """\
Eres un abogado especializado en contratos de prestación de servicios del Estado colombiano. \
Tu tarea es leer contratos y extraer ÚNICAMENTE las obligaciones ESPECÍFICAS del CONTRATISTA: \
las actividades técnicas, entregables y tareas propias del objeto contractual.

CONTEXTO IMPORTANTE: Los contratos colombianos tienen DOS tipos de obligaciones:
1. OBLIGACIONES GENERALES — deberes administrativos/legales que aplican a TODOS los contratos \
(pagar seguridad social, guardar confidencialidad, asistir a reuniones, usar EPP, etc.)
2. OBLIGACIONES ESPECÍFICAS — las actividades y tareas ÚNICAS de ESTE contrato, relacionadas \
directamente con el objeto contractual. SOLO debes extraer estas.

Si el contrato tiene una sección titulada "OBLIGACIONES ESPECÍFICAS", extrae SOLO las de esa sección.
"""

OBLIGACIONES_USER = """\
Lee el siguiente texto de un contrato de prestación de servicios y extrae SOLO las \
OBLIGACIONES ESPECÍFICAS del contratista — las actividades y tareas técnicas que debe \
desarrollar según el objeto del contrato.

REGLAS ESTRICTAS:
1. Busca en secciones que contengan obligaciones del contratista. Pueden titularse \
"OBLIGACIONES ESPECÍFICAS", "OBLIGACIONES DEL CONTRATISTA", "OBLIGACIONES PARTICULARES", \
"OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA", "CLÁUSULA DE OBLIGACIONES", "ACTIVIDADES", \
"ACTIVIDADES ESPECÍFICAS" u otras similares. \
Las obligaciones suelen venir enumeradas con números (1, 2, 3...) o letras (A, B, C...).
2. EXTRAE TODOS los ítems que estén enumerados dentro de esa sección, en el ORDEN EXACTO \
en que aparecen en el contrato. NO reordenes, NO priorices.
3. Excluye deberes administrativos/legales genéricos SOLO cuando aparezcan mezclados en \
una lista de obligaciones GENERALES (no específicas). Cuando un ítem aparece dentro de una \
sección claramente titulada "obligaciones específicas" o "actividades", inclúyelo aunque \
mencione informes, respuestas a entes de control, asistencia a reuniones técnicas, etc.
   Ejemplos de lo que SÍ debes EXCLUIR (son obligaciones generales sin sección específica):
   - Pago de seguridad social, ARL, pensión, salud
   - Confidencialidad o reserva de información (como deber genérico)
   - Uso de elementos de protección personal (EPP)
   - Pólizas de cumplimiento o garantías
   - Cumplimiento de normas ISO o reglamentos internos genéricos
   - Mantener vigente la afiliación a seguridad social
4. Transcribe TEXTUALMENTE el enunciado de la obligación tal como aparece en el contrato — \
palabra por palabra, conservando mayúsculas, puntuación interna y nombres propios. NO parafrasees, \
NO resumas, NO traduzcas. Solo elimina el numeral o viñeta inicial (ej. "1.", "a)") \
y el punto final si lo tiene. Captura también el marcador original (número o letra) en el campo etiqueta.
5. SIEMPRE incluye el ítem de cierre tipo "Las demás actividades/obligaciones que le \
sean asignadas…" si aparece en el contrato — es parte del listado oficial.
6. El ítem de cierre ("Las demás actividades/obligaciones...") es SIEMPRE el ÚLTIMO ítem \
que debes extraer. Cualquier ítem numerado que aparezca DESPUÉS de ese cierre \
(p. ej. obligaciones sobre seguridad social, software, bioseguridad, manejo de información, \
transparencia o similares) son obligaciones generales administrativas — NO las incluyas.

FORMATO DE RESPUESTA — una línea por obligación, sin texto adicional:
OBLIGACION|especifica|<etiqueta>|<descripcion>

donde <etiqueta> es el marcador original del contrato (ej. "1", "A", "a", "iii") o vacío \
si usaba viñeta/guión.

Ejemplo válido:
OBLIGACION|especifica|1|Diseñar e implementar los módulos del sistema de información según los requerimientos técnicos
OBLIGACION|especifica|2|Desarrollar las pruebas unitarias y de integración de los componentes asignados
OBLIGACION|especifica|3|Las demás actividades asignadas por la supervisión relacionadas con el objeto del contrato

TEXTO DEL CONTRATO:
\"\"\"
{texto_contrato}
\"\"\"

Responde ÚNICAMENTE con las líneas OBLIGACION|especifica|etiqueta|descripcion. Sin introducción, \
sin conclusión, sin explicaciones. Si no encuentras obligaciones específicas, \
responde con una línea vacía.
"""

# ─── Few-shot examples per entity type ──────────────────────────────────────

OBLIGACIONES_FEWSHOT_SENA = """\
### EJEMPLO — Contrato SENA (formación profesional)

TEXTO (fragmento):
\"\"\"OBLIGACIONES ESPECÍFICAS DEL CONTRATISTA: 1. Diseñar la malla curricular del programa
técnico en Análisis y Desarrollo de Software. 2. Ejecutar las sesiones de formación según
calendario SENA. 3. Elaborar los instrumentos de evaluación por competencias. 4. Registrar
notas y asistencias en el SOFIA Plus. 5. Entregar informe mensual de avance de la formación.
6. Las demás actividades que le sean asignadas por la coordinación académica y que se \
relacionen con el objeto del contrato.\"\"\"

RESPUESTA CORRECTA:
OBLIGACION|especifica|1|Diseñar la malla curricular del programa técnico en Análisis y Desarrollo de Software
OBLIGACION|especifica|2|Ejecutar las sesiones de formación según calendario SENA
OBLIGACION|especifica|3|Elaborar los instrumentos de evaluación por competencias
OBLIGACION|especifica|4|Registrar notas y asistencias en el SOFIA Plus
OBLIGACION|especifica|5|Entregar informe mensual de avance de la formación
OBLIGACION|especifica|6|Las demás actividades asignadas por la coordinación relacionadas con el objeto del contrato
"""

OBLIGACIONES_FEWSHOT_ALCALDIA = """\
### EJEMPLO — Contrato Alcaldía (secretaría de planeación)

TEXTO (fragmento):
\"\"\"OBLIGACIONES DEL CONTRATISTA: a) Realizar el diagnóstico territorial del municipio. b)
Elaborar los estudios previos para los proyectos de inversión identificados. c) Apoyar la
formulación del Plan de Desarrollo Municipal 2024-2027. d) Participar en las mesas de trabajo
convocadas por la Secretaría de Planeación. e) Presentar informe ejecutivo al Alcalde.
f) Las demás actividades que le sean asignadas por la Secretaría de Planeación y que \
guarden relación con el objeto del contrato.\"\"\"

RESPUESTA CORRECTA:
OBLIGACION|especifica|a|Realizar el diagnóstico territorial del municipio
OBLIGACION|especifica|b|Elaborar los estudios previos para los proyectos de inversión identificados
OBLIGACION|especifica|c|Apoyar la formulación del Plan de Desarrollo Municipal 2024-2027
OBLIGACION|especifica|d|Participar en las mesas de trabajo convocadas por la Secretaría de Planeación
OBLIGACION|especifica|e|Presentar informe ejecutivo al Alcalde
OBLIGACION|especifica|f|Las demás que asigne la Secretaría de Planeación relacionadas con el objeto del contrato
"""

OBLIGACIONES_FEWSHOT_MINISTERIO = """\
### EJEMPLO — Contrato Ministerio (TI / transformación digital)

TEXTO (fragmento):
\"\"\"CLÁUSULA CUARTA - OBLIGACIONES ESPECÍFICAS: 1. Liderar el proceso de levantamiento de
requerimientos del sistema de información. 2. Desarrollar los módulos funcionales según la
arquitectura aprobada. 3. Ejecutar pruebas de calidad (QA) antes de cada entrega parcial.
4. Capacitar a los usuarios finales del Ministerio. 5. Gestionar el repositorio de código
fuente en el servidor institucional. 6. Las demás actividades que le asigne la Oficina de \
Tecnología que tengan relación directa con el objeto del contrato.\"\"\"

RESPUESTA CORRECTA:
OBLIGACION|especifica|1|Liderar el proceso de levantamiento de requerimientos del sistema de información
OBLIGACION|especifica|2|Desarrollar los módulos funcionales según la arquitectura aprobada
OBLIGACION|especifica|3|Ejecutar pruebas de calidad (QA) antes de cada entrega parcial
OBLIGACION|especifica|4|Capacitar a los usuarios finales del Ministerio
OBLIGACION|especifica|5|Gestionar el repositorio de código fuente en el servidor institucional
OBLIGACION|especifica|6|Las demás que asigne la Oficina de Tecnología con relación directa al objeto del contrato
"""

OBLIGACIONES_FEWSHOT_DEFAULT = OBLIGACIONES_FEWSHOT_ALCALDIA

# Map entity_type → few-shot block
OBLIGACIONES_FEWSHOT_MAP: dict[str, str] = {
    "sena": OBLIGACIONES_FEWSHOT_SENA,
    "icbf": OBLIGACIONES_FEWSHOT_SENA,          # pedagogical, similar to SENA
    "ministerio": OBLIGACIONES_FEWSHOT_MINISTERIO,
    "dian": OBLIGACIONES_FEWSHOT_MINISTERIO,
    "alcaldia": OBLIGACIONES_FEWSHOT_ALCALDIA,
    "gobernacion": OBLIGACIONES_FEWSHOT_ALCALDIA,
    "hospital": OBLIGACIONES_FEWSHOT_ALCALDIA,
    "universidad": OBLIGACIONES_FEWSHOT_SENA,
    "entidad_publica": OBLIGACIONES_FEWSHOT_DEFAULT,
}


def get_obligaciones_fewshot(entity_type: str | None = None) -> str:
    """Return the appropriate few-shot block for a given entity type.

    Falls back to the default (alcaldía-style) if the type is unknown.
    """
    if not entity_type:
        return OBLIGACIONES_FEWSHOT_DEFAULT
    return OBLIGACIONES_FEWSHOT_MAP.get(entity_type.lower(), OBLIGACIONES_FEWSHOT_DEFAULT)

