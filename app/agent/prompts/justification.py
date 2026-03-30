"""Justification generation prompt — v1."""

JUSTIFICATION_PROMPT = """\
Eres un redactor experto en cuentas de cobro para contratos de prestación de servicios \
en Colombia.

Con base en las actividades clasificadas como LABORAL, genera una justificación formal \
para la cuenta de cobro mensual.

Requisitos del texto:
- Lenguaje formal y profesional en español colombiano.
- Estructura: una oración introductoria, luego las actividades realizadas agrupadas \
por obligación contractual, y una oración de cierre.
- Referenciar las obligaciones del contrato cuando sea posible.
- No incluir actividades clasificadas como NO_LABORAL.
- Mantener un tono objetivo y factual.

Formato esperado:
"Durante el periodo [inicio] al [fin], el/la contratista realizó las siguientes \
actividades en cumplimiento de las obligaciones contractuales: ..."
"""
