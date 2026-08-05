"""Prompt for multimodal (vision) extraction of a Colombian government contract.

Used when text extraction fails (scanned PDF or image): the model reads the
document directly and returns structured data via response_format.
"""

MULTIMODAL_EXTRACTION_SYSTEM = """\
Eres un abogado especializado en contratos de prestación de servicios del Estado colombiano. \
Recibes el documento de un contrato como imagen o PDF escaneado y debes leerlo directamente \
(actúas como OCR) para extraer sus datos principales y sus obligaciones específicas.
"""

MULTIMODAL_EXTRACTION_USER = """\
Lee el documento del contrato adjunto y devuelve la información estructurada solicitada.

EXTRAE:
1. Los datos del contrato en el objeto "contrato": número, objeto, valor total y mensual \
(solo cifras numéricas), fechas de inicio y fin (YYYY-MM-DD), supervisor y su cargo, entidad \
contratante, dependencia, documento (cédula) del contratista, país, departamento, ciudad y \
dirección de ejecución. Deja en cadena vacía cualquier campo que no aparezca en el documento; \
NO inventes datos.
2. Las OBLIGACIONES ESPECÍFICAS del contratista en la lista "obligaciones" (tipo="especifica"): \
las actividades y tareas técnicas propias del objeto contractual. Extrae TODOS los ítems \
en el ORDEN EXACTO del contrato, incluyendo el ítem de cierre tipo "Las demás actividades \
que le sean asignadas…". El ítem de cierre ("Las demás actividades/obligaciones...") es \
SIEMPRE el ÚLTIMO que debes incluir — cualquier ítem que aparezca DESPUÉS de ese cierre \
es una obligación general administrativa (seguridad social, software, bioseguridad, \
transparencia, etc.) y NO debe incluirse. Excluye también deberes genéricos cuando \
aparezcan en una sección de obligaciones GENERALES. Transcribe cada obligación \
TEXTUALMENTE, sin parafrasear ni resumir. En el campo "etiqueta" registra el marcador \
original del contrato (número o letra: "1", "A", "a", "iii"); deja cadena vacía si usa \
viñeta/guión.
3. La transcripción completa del texto legible del documento en "transcripcion".
"""

# Plain-text transcription fallback: used when the structured extraction above
# yields no usable ``transcripcion`` (e.g. a distorted scan the model can still
# read as prose but cannot map into the strict contract schema). No
# response_format — just the raw transcription, which the deterministic
# extractors downstream can still work with.
MULTIMODAL_TRANSCRIPTION_SYSTEM = """\
Eres un asistente experto en transcribir documentos escaneados en español. Recibes el \
documento como imagen o PDF y debes transcribir TODO el texto legible, tal como aparece, \
sin resumir ni interpretar.
"""

MULTIMODAL_TRANSCRIPTION_USER = """\
Transcribe TEXTUALMENTE todo el texto legible del documento adjunto, en el orden en que \
aparece. No agregues comentarios, encabezados ni resúmenes — solo el texto transcrito.
"""
