"""Prompt for extracting contract obligations from a contract document — v1."""

OBLIGACIONES_EXTRACTION_PROMPT = """\
Eres un experto en contratos de prestación de servicios del Estado colombiano.

Se te entregará el texto de un contrato. Tu tarea es identificar y extraer TODAS las \
obligaciones contractuales del contratista — tanto las generales (comunes a todo contrato \
estatal) como las específicas (propias del objeto de este contrato).

REGLAS:
- Extrae únicamente obligaciones del CONTRATISTA, no de la entidad.
- Redacta cada obligación en infinitivo o en forma nominal, de manera concisa y autocontenida.
- No repitas obligaciones aunque aparezcan en varias cláusulas.
- Clasifica como "general" si es una obligación administrativa/legal común (entregar informes, \
pagar seguridad social, guardar confidencialidad, asistir a reuniones, etc.).
- Clasifica como "especifica" si describe el trabajo técnico o la entrega concreta que se debe \
realizar según el objeto del contrato.
- Ordénalas: primero las específicas (por importancia), luego las generales.
- Si el texto es insuficiente para determinar el tipo, usa "especifica".

FORMATO DE RESPUESTA (una línea por obligación, exactamente así, sin texto adicional):
OBLIGACION|<general|especifica>|<descripcion concisa de la obligacion>

Ejemplo:
OBLIGACION|especifica|Desarrollar e implementar los módulos del sistema de información conforme a los requerimientos técnicos
OBLIGACION|especifica|Presentar al supervisor un informe mensual de actividades con los avances del período
OBLIGACION|general|Cumplir con el pago de aportes al sistema de seguridad social durante la vigencia del contrato
OBLIGACION|general|Guardar confidencialidad sobre la información a la que tenga acceso en ejercicio del contrato

Texto del contrato:
\"\"\"
{texto_contrato}
\"\"\"
"""
