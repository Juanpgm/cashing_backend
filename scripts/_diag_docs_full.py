"""Full diagnostic: what docs exist in DB vs SECOP, and what the frontend filters out."""
import asyncio
import httpx
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Contract to investigate
NUMERO_CONTRATO = "4161.010.26.1.155.2026"


async def main() -> None:
    from app.core.config import settings
    from app.core.database import async_session_factory
    from app.models.secop import SecopContrato, SecopDocumento
    from sqlalchemy import select, or_

    print("=" * 70)
    print(f"DIAGNOSTICO COMPLETO: {NUMERO_CONTRATO}")
    print("=" * 70)

    async with async_session_factory() as db:
        # 1. What's in secop_contratos?
        r = await db.execute(
            select(SecopContrato).where(
                or_(
                    SecopContrato.numero_contrato == NUMERO_CONTRATO,
                    SecopContrato.referencia_del_contrato == NUMERO_CONTRATO,
                )
            )
        )
        contratos = r.scalars().all()
        print(f"\n[1] SecOPContratos en DB: {len(contratos)}")
        for c in contratos:
            print(f"  id={c.id}")
            print(f"  numero_contrato={c.numero_contrato!r}")
            print(f"  referencia_del_contrato={c.referencia_del_contrato!r}")
            print(f"  proceso_de_compra={c.proceso_de_compra!r}")
            print(f"  estado_contrato={c.estado_contrato!r}")
            print(f"  updated_at={c.updated_at}")
            print()

        # 2. All secop_documentos for ALL processes/references
        procesos = list({c.proceso_de_compra for c in contratos if c.proceso_de_compra})
        refs = list({
            ref for c in contratos
            for ref in [c.referencia_del_contrato, c.numero_contrato]
            if ref
        })
        print(f"[2] Keys a buscar: procesos={procesos}, refs={refs}")

        conditions = []
        if refs:
            conditions.append(SecopDocumento.numero_contrato.in_(refs))
        if procesos:
            conditions.append(SecopDocumento.proceso.in_(procesos))

        if conditions:
            r2 = await db.execute(select(SecopDocumento).where(or_(*conditions)))
            docs = r2.scalars().all()
        else:
            docs = []

        print(f"\n[3] SecOPDocumentos en DB cache: {len(docs)}")

        # Identity keywords (same as frontend)
        IDENTITY_KEYWORDS = ["cedula", "cédula", "c.c.", "documento de identidad", "dni", "tarjeta de identidad"]

        def is_identity_doc(d: SecopDocumento) -> bool:
            haystack = " ".join([
                d.nombre_archivo or "",
                d.descripcion or "",
            ]).lower()
            if any(kw in haystack for kw in IDENTITY_KEYWORDS):
                return True
            name = (d.nombre_archivo or "").rsplit(".", 1)[0].strip()
            return bool(name and name.isdigit() and 6 <= len(name) <= 12)

        filtered_out = []
        shown = []
        for d in docs:
            if is_identity_doc(d):
                filtered_out.append(d)
            else:
                shown.append(d)

        print(f"  → Mostrados en frontend: {len(shown)}")
        print(f"  → Filtrados (identidad): {len(filtered_out)}")

        print("\n  Todos los documentos en cache:")
        for d in docs:
            tag = "[FILTRADO-IDENTIDAD]" if is_identity_doc(d) else ""
            print(f"  - {d.id_documento_secop[:20]:<22} ext={d.extension!r:<10} nombre={d.nombre_archivo!r:<50} proc={d.proceso!r} {tag}")

        # 3. Now query SECOP live (bypass cache)
        print("\n" + "=" * 70)
        print("[4] CONSULTA SECOP LIVE (sin cache)")
        print("=" * 70)

        headers = {"X-App-Token": settings.SECOP_APP_TOKEN}
        base_url = "https://www.datos.gov.co/resource/dmgg-8hin.json"

        all_ids: set[str] = set()
        all_rows: list[dict] = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            # Query by proceso
            for proceso in procesos:
                params = {"$where": f"proceso = '{proceso}'", "$limit": "1000"}
                r = await client.get(base_url, params=params, headers=headers)
                data = r.json() if r.status_code == 200 else []
                print(f"\n  proceso='{proceso}' → {len(data)} resultados (HTTP {r.status_code})")
                for row in data:
                    doc_id = str(row.get("id_documento") or "").strip()
                    if doc_id not in all_ids:
                        all_ids.add(doc_id)
                        all_rows.append(row)
                        print(f"    {doc_id[:20]:<22} nombre={row.get('nombre_archivo','')!r:<50} ext={row.get('extensi_n','')!r} n_contrato={row.get('n_mero_de_contrato','')!r}")

            # Query by numero/referencia
            for ref in refs:
                params = {"$where": f"n_mero_de_contrato = '{ref}'", "$limit": "1000"}
                r = await client.get(base_url, params=params, headers=headers)
                data = r.json() if r.status_code == 200 else []
                print(f"\n  n_mero_de_contrato='{ref}' → {len(data)} resultados (HTTP {r.status_code})")
                for row in data:
                    doc_id = str(row.get("id_documento") or "").strip()
                    if doc_id not in all_ids:
                        all_ids.add(doc_id)
                        all_rows.append(row)
                        print(f"    {doc_id[:20]:<22} nombre={row.get('nombre_archivo','')!r:<50} ext={row.get('extensi_n','')!r} n_contrato={row.get('n_mero_de_contrato','')!r}")

            # Also try entidad_proceso (another linking field sometimes present)
            if contratos:
                nit = contratos[0].nit_entidad if hasattr(contratos[0], 'nit_entidad') else None
                raw = contratos[0].datos_raw or {}
                print(f"\n  Raw keys in secop_contrato: {list(raw.keys())[:20]}")

            print(f"\n[5] TOTAL UNICO SECOP LIVE: {len(all_ids)} documentos")

        # 4. Show ALL raw fields of first doc to understand dataset schema
        if all_rows:
            print("\n[6] Ejemplo de fila cruda (primer doc):")
            import json
            print(json.dumps(all_rows[0], indent=2, ensure_ascii=False, default=str))


asyncio.run(main())
