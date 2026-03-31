"""Prompt for extracting contract obligations from a Colombian government contract — v2."""

OBLIGACIONES_SYSTEM = """\
Eres un abogado especializado en contratos de prestación de servicios del Estado colombiano. \
Tu tarea es leer contratos y extraer con precisión todas las obligaciones del CONTRATISTA, \
clasificarlas y devolverlas en un formato estructurado exacto.
"""

OBLIGACIONES_USER = """\
Lee el siguiente texto de un contrato de prestación de servicios y extrae TODAS las \
obligaciones del contratista que encuentres.

INSTRUCCIONES:
1. Busca las obligaciones en cláusulas tituladas: "OBLIGACIONES DEL CONTRATISTA", \
"OBLIGACIONES ESPECÍFICAS", "OBLIGACIONES GENERALES", "CLÁUSULA DE OBLIGACIONES", \
o cualquier sección que liste deberes del contratista.
2. También extrae obligaciones implícitas en el objeto del contrato y en el alcance del trabajo.
3. Clasifica cada obligación:
   - "especifica": trabajo técnico, entregables, actividades propias del objeto del contrato
   - "general": deberes administrativos/legales (informes, seguridad social, confidencialidad, \
reuniones, disponibilidad, etc.)
4. Redacta en infinitivo o forma nominal, concisa y autocontenida.
5. No omitas ninguna obligación aunque parezca obvia o repetida en otra cláusula.
6. Ordena: primero las específicas (por importancia), luego las generales.

FORMATO DE RESPUESTA — una línea por obligación, sin texto adicional antes ni después:
OBLIGACION|especifica|<descripcion>
OBLIGACION|general|<descripcion>

Ejemplo válido:
OBLIGACION|especifica|Diseñar e implementar los módulos del sistema de información según los requerimientos técnicos del supervisor
OBLIGACION|especifica|Presentar informe mensual de actividades con soportes al supervisor dentro de los primeros cinco días del mes siguiente
OBLIGACION|general|Cumplir con el pago de aportes al sistema de seguridad social integral durante toda la vigencia del contrato
OBLIGACION|general|Mantener confidencialidad sobre la información institucional a la que tenga acceso durante la ejecución del contrato

TEXTO DEL CONTRATO:
\"\"\"
{texto_contrato}
\"\"\"

Responde ÚNICAMENTE con las líneas OBLIGACION|tipo|descripcion. Sin introducción, sin conclusión, \
sin numeración, sin explicaciones.
"""
