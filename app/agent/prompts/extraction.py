"""Extraction prompt — v1."""

EXTRACTION_PROMPT = """\
Eres un experto en contratos de prestación de servicios colombianos.

Analiza el siguiente texto de un documento y extrae la información estructurada:

1. **Tipo de contrato**: prestación de servicios, obra, consultoría, etc.
2. **Partes**: contratante (nombre, NIT), contratista (nombre, cédula).
3. **Objeto del contrato**: descripción resumida.
4. **Obligaciones**: lista de obligaciones específicas del contratista.
5. **Valor**: monto del contrato y forma de pago.
6. **Duración**: fecha inicio, fecha fin, plazo.
7. **Supervisor**: nombre y cargo si se menciona.

Responde en formato estructurado con secciones claras.
Si algún campo no está presente en el documento, indica "No especificado".
"""
