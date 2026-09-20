---
name: gotcha-enum-labels-postgres
description: "En este backend NO todos los enums Postgres usan el mismo casing de labels: depende de si el mapped_column pasa values_callable o no. Verificar antes de escribir cualquier migración de enum."
metadata: 
  node_type: memory
  type: project
  originSessionId: 819103c3-54f6-473b-bfed-82c9019f1192
  modified: 2026-09-03T04:42:42.291Z
---

Regla afinada 2026-09-02 tras casi meter una migración rota. La nota vieja decía solo "los enums Postgres acá usan NOMBRES como labels" — es cierto para algunos y falso para otros, y esa media verdad es peligrosa.

**El mecanismo, no la anécdota:** SQLAlchemy decide el label del enum en Postgres según cómo se declara el `mapped_column`.

En `app/models/documento_fuente.py`, el MISMO modelo tiene los dos casos:

```python
# línea 56 — SIN values_callable → labels = NOMBRES de los miembros, en MAYÚSCULA
Enum(TipoDocumentoFuente, name="tipo_documento_fuente")
# → 'CONTRATO', 'INSTRUCCIONES', 'PLANTILLA', 'RPC', ...

# línea 63 — CON values_callable → labels = VALORES, en minúscula
Enum(CategoriaDocumento, name="categoria_documento", values_callable=lambda x: [e.value for e in x])
```

**Por qué importa:** una migración con `ALTER TYPE ... ADD VALUE 'otros'` sobre `tipo_documento_fuente` corre sin error, se marca como aplicada, y después el ORM inserta `'OTROS'` → revienta en runtime, en producción, lejos del cambio que lo causó.

**Trampa adicional:** la migración `011_requisitos_documentos.py:193` agregó sus labels en MINÚSCULA sobre `tipo_documento_fuente`, o sea que el precedente que está en el repo es inconsistente con lo que el ORM emite. No copiar 011 a ciegas. La migración `029` agrega ambos casings bajo `ADD VALUE IF NOT EXISTS` (el redundante es no-op) y explica el porqué en su docstring.

**Regla operativa:** antes de escribir una migración de enum, abrir el `mapped_column` del modelo y mirar si pasa `values_callable`. No inferirlo del precedente de otra migración, ni de otro enum del mismo archivo.

**Why:** yo mismo briefeé mal a un subagente con la versión simplificada de esta nota; lo salvó que fue a verificar contra la base en vez de creerme.
**How to apply:** vale también al revés — si agregás `values_callable` a un enum existente, cambiás el casing de TODOS sus labels y necesitás migración de datos. Ver [[bug-web-borra-contrato]].
