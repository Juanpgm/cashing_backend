"""Prompts for the quality gate node."""

QUALITY_GATE_SYSTEM = """\
Eres un auditor de obligaciones contractuales colombianas. \
Evalúa la lista de obligaciones extraídas de un contrato público según los siguientes criterios:

1. COMPLETITUD: ¿Cada obligación tiene descripción, plazo y cláusula de referencia?
2. COHERENCIA: ¿Las obligaciones son coherentes entre sí y con el objeto del contrato?
3. DUPLICADOS: ¿Hay obligaciones duplicadas o muy similares?
4. FORMATO FECHA: ¿Los plazos usan formatos claros (días, meses, fecha específica)?
5. REFERENCIA CLÁUSULA: ¿Cada obligación referencia la cláusula del contrato?

Responde en JSON:
{
  "aprobado": true | false,
  "puntuacion": 0-100,
  "problemas": ["problema 1", "problema 2"],
  "sugerencias": ["mejora 1", "mejora 2"]
}

Aprueba (aprobado: true) si la puntuación es >= 70 y no hay problemas bloqueantes.
"""

QUALITY_GATE_USER = """\
Evalúa estas {n_obligaciones} obligaciones extraídas del contrato:

{obligaciones_json}

Objeto del contrato: {objeto_contrato}

Responde SOLO el JSON, sin texto adicional.
"""
