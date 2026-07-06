"""Prompt for extracting contract metadata from a Colombian government contract — v2.

Uses response_format=ContratoCamposLLM (structured JSON output).
The prompt instructs WHAT to extract; the schema enforces the JSON shape.
"""

CONTRATO_EXTRACTION_SYSTEM = """\
Eres un abogado especializado en contratos de prestación de servicios del Estado colombiano. \
Tu tarea es leer el texto de un contrato y extraer con precisión los datos principales. \
Los contratos colombianos tienen estructuras variables según la entidad, \
pero siempre contienen número de contrato, objeto, valor, fechas y datos del supervisor. \
Debes buscar estos datos aunque estén dispersos a lo largo del documento o en cláusulas con nombres distintos.
"""

CONTRATO_EXTRACTION_USER = """\
Lee el siguiente texto de un contrato de prestación de servicios colombiano y extrae los datos indicados.

CAMPOS A EXTRAER:

1. **numero_contrato**: El identificador único del contrato. Puede aparecer como:
   - "Contrato N° CPS-2024-001", "CD-045-2025", "Contrato de Prestación de Servicios No. 123 de 2024"
   - También puede llamarse: No. de contrato, Número de contrato, Referencia del contrato
   - Extrae solo el código/número (ej: "CPS-2024-001", "CD-045-2025", "123-2024")

2. **objeto**: La descripción del servicio a prestar. Busca la cláusula u artículo llamado \
"OBJETO" o "CLÁUSULA PRIMERA" o "ALCANCE". Es el párrafo que describe qué debe hacer el contratista.

3. **valor_total**: El valor total del contrato en cifra numérica (sin puntos de miles, \
con punto decimal si aplica). Busca: "VALOR DEL CONTRATO", "CLÁUSULA … VALOR", "por la suma de $…".

4. **valor_mensual**: El valor de cada pago mensual o período. Si no se menciona explícitamente, \
divide el valor_total entre el número de meses de duración del contrato.

5. **fecha_inicio**: Fecha de inicio en formato YYYY-MM-DD. Busca: "fecha de inicio", \
"inicio de ejecución", "a partir del día…", en la cláusula de PLAZO o VIGENCIA.

6. **fecha_fin**: Fecha de terminación en formato YYYY-MM-DD. Busca: "fecha de terminación", \
"hasta el día…", plazo de N meses contados desde la fecha de inicio.

7. **supervisor_nombre**: Nombre completo del supervisor del contrato. Busca la cláusula \
de SUPERVISIÓN o INTERVENTORÍA. Puede llamarse supervisor, interventor, o coordinador.

8. **cargo_supervisor**: Cargo o título del supervisor (ej: "Jefe de Oficina Asesora", \
"Director de Área", "Coordinador de Proyectos").

9. **entidad**: Nombre de la entidad pública contratante (ej: "Ministerio de Tecnologías de \
la Información y las Comunicaciones", "Departamento Nacional de Planeación").

10. **dependencia**: La dependencia, dirección, oficina o área de la entidad que supervisa \
el contrato. No confundir con el departamento geográfico.

11. **documento_proveedor**: Número de cédula o NIT del contratista/proveedor (persona natural o empresa).

12. **pais**: País de ejecución (generalmente "Colombia").

13. **departamento**: Departamento geográfico de ejecución (ej: "Bogotá D.C.", "Cundinamarca", \
"Antioquia").

14. **ciudad**: Ciudad o municipio de ejecución del contrato.

15. **direccion_ejecucion**: Dirección física del lugar de ejecución si aparece explícitamente.

REGLAS:
- Deja en cadena vacía ("") cualquier campo que no encuentres en el texto.
- NO inventes datos. Si el número de contrato no aparece claramente, deja número vacío.
- Para los valores monetarios: extrae solo la cifra (ej: "12000000" no "$12.000.000").
- Para las fechas: convierte siempre a formato YYYY-MM-DD.
- Si el plazo está expresado en meses (ej: "por el término de 6 meses"), calcula la fecha de fin \
sumando los meses a la fecha de inicio.
- Si el objeto es largo, inclúyelo completo — es el campo más importante.

FORMATO DE RESPUESTA (elige uno):
Opción A — JSON (preferido):
{{"numero_contrato": "...", "objeto": "...", "valor_total": "...", "valor_mensual": "...", \
"fecha_inicio": "...", "fecha_fin": "...", "supervisor_nombre": "...", "cargo_supervisor": "...", \
"entidad": "...", "dependencia": "...", "documento_proveedor": "...", \
"pais": "...", "departamento": "...", "ciudad": "...", "direccion_ejecucion": "..."}}

Opción B — una línea por campo (si no podés responder en JSON):
CAMPO|numero_contrato|<valor>
CAMPO|objeto|<valor>
CAMPO|valor_total|<valor>
CAMPO|valor_mensual|<valor>
CAMPO|fecha_inicio|<valor>
CAMPO|fecha_fin|<valor>
CAMPO|supervisor_nombre|<valor>
CAMPO|cargo_supervisor|<valor>
CAMPO|entidad|<valor>
CAMPO|dependencia|<valor>
CAMPO|documento_proveedor|<valor>
CAMPO|pais|<valor>
CAMPO|departamento|<valor>
CAMPO|ciudad|<valor>
CAMPO|direccion_ejecucion|<valor>

TEXTO DEL CONTRATO:
\"\"\"
{texto_contrato}
\"\"\"
"""
